"""pytest 全局配置：把 DATA_DIR 指到进程级临时目录。

为什么必须在这里设（早于一切测试模块的 import）：
- core.config 在 import 时读取 DATA_DIR，未设置时默认「项目根目录」→ db.session 的
  引擎会绑定到仓库里的真实 app.db；
- 各测试文件的 `os.environ["DATA_DIR"] = mkdtemp()` 都在模块 import 阶段执行，
  而 pytest 按字母序先 import test_admin_lan_only / test_device_binding，
  它们（经 core.config / core.device_binding）先触发 db.session 建引擎——
  届时谁的临时目录都还没设，引擎就绑到了生产 app.db，
  后续所有「共享 db.session 引擎」的集成测试（device_binding / 下载槽 e2e 等）
  全部落进生产库，跨次运行还会撞 UNIQUE 约束。

本文件在 pytest 收集任何测试模块之前 import，先占住 DATA_DIR，
所有共享引擎的集成测试因此落在「每个 pytest 进程全新」的临时目录里，
互不干扰、不碰生产 app.db。
（各自建独立 engine 的测试文件，如 test_donor_cooldown / test_user_donate_pool，
  不受此影响。）
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pytest_data_")
