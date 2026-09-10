"""Control-plane routers.

Archive skill routes extend the existing Skills router so the app keeps a single
Skills registration point and OpenAPI tag group.
"""

from control_plane.routers import skill_archives as _skill_archives
from control_plane.routers import skills as _skills

_skills.router.include_router(_skill_archives.router)
