"""按主题拆分的路由包。

依赖方向是单向的：`lab_generator` / `auth` ← `deps` ← `routers` ← `main`。
路由模块**不允许** import main —— 那会造成循环导入。
"""
