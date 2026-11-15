"""Flask 应用工厂与路由。"""
from __future__ import annotations

import json
from collections import defaultdict

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, url_for)

from ..config import SERVER_REGISTRY
from ..db import connect, init_db
from ..evaluation import evaluate
from ..ingest.bibtex import export_person_bibtex, export_work_bibtex
from ..ingest.disambiguation import Disambiguator
from ..ingest.store import IngestStore
from ..ingest.works import WorkLinker
from ..oai.harvester import Harvester


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "scholar-hub-local-demo"

    def db():
        conn = connect(db_path)
        init_db(conn)
        return conn

    # ------------------------------------------------------------ 首页
    @app.route("/")
    def index():
        conn = db()
        stats = {
            "repositories": conn.execute(
                "SELECT COUNT(*) n FROM repositories").fetchone()["n"],
            "records": conn.execute(
                "SELECT COUNT(*) n FROM records").fetchone()["n"],
            "works": conn.execute(
                "SELECT COUNT(*) n FROM works").fetchone()["n"],
            "persons": conn.execute(
                "SELECT COUNT(*) n FROM persons").fetchone()["n"],
        }
        conn.close()
        return render_template("index.html", stats=stats)

    # ------------------------------------------------------------ 仓储登记
    @app.route("/repositories")
    def repositories():
        conn = db()
        rows = conn.execute(
            "SELECT * FROM repositories ORDER BY code").fetchall()
        servers_on_disk = []
        if SERVER_REGISTRY.exists():
            servers_on_disk = json.loads(SERVER_REGISTRY.read_text(
                encoding="utf-8"))
        registered = {r["code"] for r in rows}
        conn.close()
        return render_template("repositories.html", rows=rows,
                               servers=servers_on_disk,
                               registered=registered)

    @app.route("/repositories/register", methods=["POST"])
    def register_repo():
        conn = db()
        IngestStore(conn).upsert_repository({
            "code": request.form["code"].strip(),
            "name": request.form["name"].strip(),
            "base_url": request.form["base_url"].strip(),
            "metadata_format": request.form["metadata_format"],
            "set_spec": request.form.get("set_spec") or None,
            "page_size": int(request.form.get("page_size") or 25),
            "quirks_json": request.form.get("quirks_json") or "[]",
        })
        conn.close()
        flash("仓储已登记", "ok")
        return redirect(url_for("repositories"))

    @app.route("/repositories/register-disk/<code>", methods=["POST"])
    def register_from_disk(code):
        item = next((s for s in json.loads(SERVER_REGISTRY.read_text(
            encoding="utf-8")) if s["code"] == code), None)
        if item is None:
            abort(404)
        conn = db()
        IngestStore(conn).upsert_repository({
            "code": item["code"], "name": item["name"],
            "base_url": item["base_url"],
            "metadata_format": item["metadata_format"],
            "set_spec": item["set_spec"], "page_size": item["page_size"],
            "quirks_json": json.dumps(item["quirks"], ensure_ascii=False),
        })
        conn.close()
        flash(f"已从 servers.json 登记 {code}", "ok")
        return redirect(url_for("repositories"))

    @app.route("/repositories/<int:repo_id>/toggle", methods=["POST"])
    def toggle_repo(repo_id):
        conn = db()
        conn.execute("UPDATE repositories SET is_active=1-is_active WHERE id=?",
                     (repo_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("repositories"))

    # ------------------------------------------------------------ 收割
    @app.route("/harvest")
    def harvest_view():
        conn = db()
        repos = conn.execute("SELECT * FROM repositories ORDER BY code").fetchall()
        runs = conn.execute(
            """SELECT hr.*, r.code, r.name AS repo_name FROM harvest_runs hr
               JOIN repositories r ON r.id=hr.repository_id
               ORDER BY hr.id DESC LIMIT 60""").fetchall()
        checkpoints = conn.execute(
            "SELECT * FROM harvest_checkpoints").fetchall()
        cp_by_repo = {c["repository_id"]: c for c in checkpoints}
        conn.close()
        return render_template("harvest.html", repos=repos, runs=runs,
                               cp_by_repo=cp_by_repo)

    @app.route("/harvest/run", methods=["POST"])
    def harvest_run():
        repo_id = int(request.form["repository_id"])
        from_date = request.form.get("from_date") or None
        until_date = request.form.get("until_date") or None
        conn = db()
        repo = IngestStore(conn).get_repository(repo_id)
        summary = Harvester(conn).harvest_repository(
            repo, from_date=from_date, until_date=until_date)
        WorkLinker(conn).rebuild()
        conn.close()
        if summary.status == "completed":
            flash(f"收割完成：新增 {summary.inserted}，更新 {summary.updated}，"
                  f"删除 {summary.deleted}，令牌过期回退 {summary.token_expired} 次",
                  "ok")
        else:
            flash(f"收割中断（已保留断点）：{summary.error}", "err")
        return redirect(url_for("harvest_view"))

    @app.route("/harvest/run-all", methods=["POST"])
    def harvest_run_all():
        from_date = request.form.get("from_date") or None
        conn = db()
        store = IngestStore(conn)
        totals = defaultdict(int)
        for repo in store.list_repositories():
            if not repo["is_active"]:
                continue
            s = Harvester(conn).harvest_repository(repo, from_date=from_date)
            totals["inserted"] += s.inserted
            totals["updated"] += s.updated
            totals["deleted"] += s.deleted
            totals["token_expired"] += s.token_expired
        WorkLinker(conn).rebuild()
        conn.close()
        flash("全部仓储收割完成：新增 {inserted}，更新 {updated}，删除 {deleted}，"
              "令牌回退 {token_expired} 次".format(**totals), "ok")
        return redirect(url_for("harvest_view"))

    # ------------------------------------------------------------ 记录浏览
    @app.route("/records")
    def records_view():
        conn = db()
        q = request.args.get("q", "").strip()
        doi = request.args.get("doi", "").strip().lower()
        repo_id = request.args.get("repo_id", "").strip()
        sql = """SELECT r.*, rp.code AS repo_code FROM records r
                 JOIN repositories rp ON rp.id=r.repository_id WHERE 1=1"""
        params: list = []
        if q:
            sql += " AND r.title LIKE ?"
            params.append(f"%{q}%")
        if doi:
            sql += " AND LOWER(r.doi) LIKE ?"
            params.append(f"%{doi}%")
        if repo_id:
            sql += " AND r.repository_id=?"
            params.append(repo_id)
        sql += " ORDER BY r.datestamp DESC LIMIT 300"
        rows = list(conn.execute(sql, params).fetchall())
        repos = list(conn.execute(
            "SELECT id, code FROM repositories ORDER BY code"))
        conn.close()
        return render_template("records.html", rows=rows, repos=repos,
                               q=q, doi=doi, repo_id=repo_id)

    @app.route("/records/<int:record_id>")
    def record_detail(record_id):
        conn = db()
        rec = conn.execute(
            """SELECT r.*, rp.code AS repo_code FROM records r
               JOIN repositories rp ON rp.id=r.repository_id
               WHERE r.id=?""", (record_id,)).fetchone()
        if rec is None:
            abort(404)
        creators = conn.execute(
            "SELECT * FROM record_creators WHERE record_id=? ORDER BY position",
            (record_id,)).fetchall()
        subjects = conn.execute(
            "SELECT subject FROM record_subjects WHERE record_id=?",
            (record_id,)).fetchall()
        dois = conn.execute(
            "SELECT * FROM record_dois WHERE record_id=?",
            (record_id,)).fetchall()
        work = conn.execute(
            "SELECT w.* FROM record_work rw JOIN works w ON w.id=rw.work_id "
            "WHERE rw.record_id=?", (record_id,)).fetchone()
        siblings = []
        if work:
            siblings = conn.execute(
                """SELECT r.id, r.title, r.type, r.doi, r.date FROM record_work rw
                   JOIN records r ON r.id=rw.record_id
                   WHERE rw.work_id=? AND r.id!=? ORDER BY r.date DESC""",
                (work["id"], record_id)).fetchall()
        events = conn.execute(
            "SELECT * FROM record_events WHERE repository_id=? "
            "AND oai_identifier=? ORDER BY id DESC LIMIT 20",
            (rec["repository_id"], rec["oai_identifier"])).fetchall()
        conn.close()
        return render_template("record_detail.html", rec=rec,
                               creators=creators, subjects=subjects,
                               dois=dois, work=work, siblings=siblings,
                               events=events)

    @app.route("/works/<int:work_id>/bibtex")
    def work_bibtex(work_id):
        conn = db()
        text = export_work_bibtex(conn, work_id)
        conn.close()
        return Response(text, mimetype="application/x-bibtex",
                        headers={"Content-Disposition":
                                 f"attachment; filename=work{work_id}.bib"})

    # ------------------------------------------------------------ 作者归并
    @app.route("/authors")
    def authors_view():
        conn = db()
        q = request.args.get("q", "").strip()
        sql = """SELECT p.*, COUNT(rc.id) AS mention_count
                 FROM persons p
                 LEFT JOIN record_creators rc ON rc.person_id=p.id
                 GROUP BY p.id HAVING 1=1"""
        params: list = []
        if q:
            sql += " AND p.display_name LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY mention_count DESC, p.id LIMIT 300"
        persons = conn.execute(sql, params).fetchall()
        conn.close()
        return render_template("authors.html", persons=persons, q=q)

    @app.route("/authors/<int:person_id>")
    def author_detail(person_id):
        conn = db()
        person = conn.execute("SELECT * FROM persons WHERE id=?",
                              (person_id,)).fetchone()
        if person is None:
            abort(404)
        mentions = conn.execute(
            """SELECT rc.*, r.title AS record_title, r.id AS record_id,
                      r.date, rp.code AS repo_code
               FROM record_creators rc
               JOIN records r ON r.id=rc.record_id
               JOIN repositories rp ON rp.id=r.repository_id
               WHERE rc.person_id=? ORDER BY r.date DESC, rc.id""",
            (person_id,)).fetchall()
        locks = conn.execute(
            "SELECT * FROM mention_locks WHERE person_id=?",
            (person_id,)).fetchall()
        constraints = conn.execute(
            "SELECT * FROM pair_constraints WHERE person_a=? OR person_b=?",
            (person_id, person_id)).fetchall()
        candidates = conn.execute(
            "SELECT id, display_name FROM persons WHERE id!=? "
            "ORDER BY display_name LIMIT 500", (person_id,)).fetchall()
        conn.close()
        return render_template("author_detail.html", person=person,
                               mentions=mentions, locks=locks,
                               constraints=constraints,
                               candidates=candidates)

    @app.route("/authors/merge", methods=["POST"])
    def authors_merge():
        keep = int(request.form["keep_id"])
        drop = int(request.form["drop_id"])
        conn = db()
        Disambiguator(conn).merge_persons(
            keep, drop, note=request.form.get("note") or "手工合并")
        conn.close()
        flash(f"已合并到学者 #{keep}", "ok")
        return redirect(url_for("author_detail", person_id=keep))

    @app.route("/authors/<int:person_id>/split", methods=["POST"])
    def authors_split(person_id):
        keys = request.form.getlist("mention_keys")
        conn = db()
        new_id = Disambiguator(conn).split_person(
            person_id, keys, note=request.form.get("note") or "手工拆分")
        conn.close()
        flash(f"已拆分出新学者 #{new_id}，并建立 cannot_link 约束", "ok")
        return redirect(url_for("author_detail", person_id=person_id))

    @app.route("/authors/<int:person_id>/assign", methods=["POST"])
    def authors_assign(person_id):
        key = request.form["mention_key"]
        target = int(request.form["target_person_id"])
        conn = db()
        Disambiguator(conn).assign_mention(
            key, target, note=request.form.get("note") or "手工指派")
        conn.close()
        flash("提及已指派并加锁", "ok")
        return redirect(url_for("author_detail", person_id=person_id))

    @app.route("/authors/rerun", methods=["POST"])
    def authors_rerun():
        conn = db()
        stats = Disambiguator(conn).run()
        conn.close()
        flash(f"自动归并完成：{stats['persons']} 位学者，"
              f"{stats['merges']} 次自动合并（手工锁已保留）", "ok")
        return redirect(url_for("authors_view"))

    # ------------------------------------------------------------ 学者主页
    @app.route("/scholars/<int:person_id>")
    def scholar_page(person_id):
        conn = db()
        person = conn.execute("SELECT * FROM persons WHERE id=?",
                              (person_id,)).fetchone()
        if person is None:
            abort(404)
        works = conn.execute(
            """SELECT w.*, MIN(r.date) AS first_date,
                      GROUP_CONCAT(DISTINCT r.type) AS type_mix
               FROM record_work rw
               JOIN records r ON r.id=rw.record_id
               JOIN works w ON w.id=rw.work_id
               WHERE rw.work_id IN (
                   SELECT rw2.work_id FROM record_work rw2
                   JOIN record_creators rc ON rc.record_id=rw2.record_id
                   WHERE rc.person_id=?)
               GROUP BY w.id ORDER BY COALESCE(w.year, '') DESC, w.id""",
            (person_id,)).fetchall()
        by_year: dict[str, list] = defaultdict(list)
        for w in works:
            year = (w["year"] or (w["first_date"] or "")[:4] or "未知")
            by_year[year].append(w)
        coauthors = conn.execute(
            """SELECT p.id, p.display_name, COUNT(*) c
               FROM record_creators rc
               JOIN records r ON r.id=rc.record_id
               JOIN record_creators peer ON peer.record_id=r.id
               JOIN persons p ON p.id=peer.person_id
               WHERE rc.person_id=? AND peer.person_id!=?
               GROUP BY p.id ORDER BY c DESC LIMIT 12""",
            (person_id, person_id)).fetchall()
        subjects = conn.execute(
            """SELECT rs.subject, COUNT(*) c FROM record_subjects rs
               JOIN record_creators rc ON rc.record_id=rs.record_id
               WHERE rc.person_id=? GROUP BY rs.subject
               ORDER BY c DESC LIMIT 10""", (person_id,)).fetchall()
        conn.close()
        return render_template("scholar.html", person=person,
                               by_year=dict(sorted(by_year.items(),
                                                   reverse=True)),
                               coauthors=coauthors, subjects=subjects)

    @app.route("/scholars/<int:person_id>/bibtex")
    def scholar_bibtex(person_id):
        conn = db()
        text = export_person_bibtex(conn, person_id)
        conn.close()
        return Response(text, mimetype="application/x-bibtex",
                        headers={"Content-Disposition":
                                 f"attachment; filename=scholar{person_id}.bib"})

    # ------------------------------------------------------------ 评估
    @app.route("/evaluation")
    def evaluation_view():
        conn = db()
        try:
            metrics = evaluate(conn)
        except FileNotFoundError:
            metrics = None
        conn.close()
        return render_template("evaluation.html", metrics=metrics)

    return app
