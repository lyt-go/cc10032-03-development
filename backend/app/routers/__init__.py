"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, retention_cleanup

# retention_cleanup 为固定前缀路由，排在通用动态路由 /api/{module} 之前注册
ROUTERS = [retention_cleanup, resource]
