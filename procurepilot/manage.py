"""数据库维护命令：使用 DATABASE_URL 指定待迁移的数据库。"""

import argparse
import json
from sqlalchemy import text
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["migrate", "seed-graph", "warmup-search", "migrate-objects"]
    )
    parser.add_argument("--apply", action="store_true", help="提交迁移后的 S3 对象引用")
    parser.add_argument("--limit", type=int, default=100, help="单批迁移的最大附件数")
    args = parser.parse_args()
    if args.command == "warmup-search":
        import os
        from .search_index import embed, MODEL, MODEL_REVISION

        os.environ["ALLOW_MODEL_DOWNLOAD"] = "1"
        vectors = embed(["固定合成采购参数，验证本机向量模型"])
        print(
            {"model": MODEL, "revision": MODEL_REVISION, "dimension": len(vectors[0])}
        )
        return
    if args.command == "seed-graph":
        from .graph import seed_fixtures

        print(seed_fixtures())
        return
    store = Store()
    try:
        if args.command == "migrate-objects":
            from .object_migration import migrate

            result = migrate(store, limit=args.limit, apply=args.apply)
            print(json.dumps(result, ensure_ascii=False))
            if result["failed"]:
                raise SystemExit(1)
        else:
            with store.engine.connect() as connection:
                print(
                    "Schema revision:",
                    connection.scalar(text("select version_num from alembic_version")),
                )
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
