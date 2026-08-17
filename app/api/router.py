from fastapi import APIRouter

from app.api import admin, auth, cart, catalog, media, messaging, owner, profile, push, qr

api_router = APIRouter()
api_router.include_router(admin.router)
api_router.include_router(auth.router)
api_router.include_router(qr.router)
api_router.include_router(catalog.router)
api_router.include_router(cart.router)
api_router.include_router(profile.router)
api_router.include_router(messaging.router)
api_router.include_router(push.router)
api_router.include_router(media.router)
api_router.include_router(owner.router)
