"""OpenMAIC control-plane boundaries owned by the teaching domain."""

from deeptutor.teaching.openmaic.auth import (
    MountedServiceSecretResolver,
    ServiceRequest,
)
from deeptutor.teaching.openmaic.client import (
    OpenMAICClient,
    OpenMAICClientFactory,
)

__all__ = [
    "MountedServiceSecretResolver",
    "OpenMAICClient",
    "OpenMAICClientFactory",
    "ServiceRequest",
]
