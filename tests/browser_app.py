"""浏览器验收专用服务：仅测试导入，生产启动绝不能使用此入口。

延迟模型桩让浏览器实际经历排队、执行、刷新、取消，完全不访问外部模型。
数据库路径由测试命令单独指定，不接触用户业务库。
"""

import os
import time
from procurepilot import analysis
from procurepilot.api import create_app
from .test_models import FakeModel

if os.getenv("PROCUREPILOT_BROWSER_TEST") != "1":
    raise RuntimeError("浏览器测试入口必须显式启用，并指定隔离数据库")
if not os.getenv("DATABASE_URL", "").endswith("browser05.db"):
    raise RuntimeError("浏览器测试只能使用独立 browser05.db")


class SlowModel(FakeModel):
    def assess(self, *args):
        time.sleep(1.5)
        return super().assess(*args)


for variable in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"):
    os.environ[variable] = "browser-test-fake-model"
os.environ["MODEL_WORKER_MODE"] = "embedded"
analysis.ChatCompletionsModel = SlowModel
app = create_app()
