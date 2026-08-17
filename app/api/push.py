from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from app.dependencies import CurrentPrincipal, DBSession
from app.schemas.push import PushDeviceRegistration, PushDeviceResponse
from app.services import push as push_service

router = APIRouter(prefix="/push/devices", tags=["push notifications"])

InstallationID = Annotated[
    str,
    Path(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
]


@router.put("/{installation_id}", response_model=PushDeviceResponse)
def put_push_device(
    installation_id: InstallationID,
    payload: PushDeviceRegistration,
    db: DBSession,
    principal: CurrentPrincipal,
) -> PushDeviceResponse:
    return push_service.register_device(
        db,
        user=principal.user,
        auth_session=principal.session,
        installation_id=installation_id,
        payload=payload,
    )


@router.delete("/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_device(
    installation_id: InstallationID,
    db: DBSession,
    principal: CurrentPrincipal,
) -> Response:
    push_service.unregister_device(
        db,
        user=principal.user,
        installation_id=installation_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
