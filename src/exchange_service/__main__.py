"""`python -m exchange_service`：启动 HTTP 服务，启动前先执行恢复清理。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchange_service.api import make_server
from exchange_service.seed import seed
from exchange_service.service import ExchangeService
from exchange_service.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="中外教师互访编排服务端")
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入演示种子数据")
    args = parser.parse_args()

    store = Store(args.db)
    if args.seed:
        seed(store)
    service = ExchangeService(store)
    # 进程恢复：继续清理上次运行遗留的逾期暂占
    report = service.recover()
    print("恢复清理：" + json.dumps(report, ensure_ascii=False), file=sys.stderr)

    server = make_server(service, args.host, args.port)
    print(f"互访编排服务已启动：http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
