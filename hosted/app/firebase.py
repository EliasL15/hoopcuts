from __future__ import annotations

from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def admin_app():
    import firebase_admin

    try:
        return firebase_admin.get_app()
    except ValueError:
        options = {"projectId": settings.firebase_project_id} if settings.firebase_project_id else {}
        return firebase_admin.initialize_app(options=options or None)


@lru_cache(maxsize=1)
def client():
    from firebase_admin import firestore

    return firestore.client(app=admin_app())
