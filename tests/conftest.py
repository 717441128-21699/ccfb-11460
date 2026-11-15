"""pytest 全局夹具：生成数据、启动本地仓储、提供数据库连接。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from scholar_hub import config as sh_config
from scholar_hub.data_gen import build_dataset, write_dataset
from scholar_hub.db import connect, init_db, reset_db
from scholar_hub.ingest.store import IngestStore
from scholar_hub.repositories.server import ServerManager


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    ds = build_dataset(seed=42)
    write_dataset(ds, out_dir=data_dir)
    # 让所有模块的默认真值目录指向本测试的临时数据
    sh_config.GEN_DATA_DIR = data_dir
    from scholar_hub import evaluation, ingest
    evaluation.GEN_DATA_DIR = data_dir
    ingest.disambiguation.GEN_DATA_DIR = data_dir
    return ds, data_dir


@pytest.fixture(scope="session")
def servers(dataset):
    _, data_dir = dataset
    manager = ServerManager(data_dir=data_dir)
    server_list = manager.start()
    time.sleep(0.4)
    yield server_list
    manager.stop()


@pytest.fixture()
def conn(dataset, servers, tmp_path):
    _, data_dir = dataset
    db_path = tmp_path / "test.db"
    reset_db(db_path)
    c = connect(db_path)
    init_db(c)
    for item in json.loads((data_dir / "servers.json").read_text(
            encoding="utf-8")):
        IngestStore(c).upsert_repository({
            "code": item["code"], "name": item["name"],
            "base_url": item["base_url"],
            "metadata_format": item["metadata_format"],
            "set_spec": item["set_spec"], "page_size": item["page_size"],
            "quirks_json": json.dumps(item["quirks"], ensure_ascii=False),
        })
    yield c
    c.close()
