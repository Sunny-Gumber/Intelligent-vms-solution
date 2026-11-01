"""Adapter registry — vendor integrations stay isolated.

Resolution order:
  1. Explicit `adapter` name on the camera row (operator override)
  2. Vendor-specific adapter registered for the detected manufacturer
  3. Protocol default: onvif or rtsp

Vendor adapters SUBCLASS OnvifAdapter/RtspAdapter and may only override
behaviour. They are never imported from core modules, so removing a vendor
integration can never break the product.
"""

from __future__ import annotations

from app.devices.base import Credentials, DeviceAdapter, DeviceEndpoint
from app.devices.onvif_adapter import OnvifAdapter
from app.devices.rtsp_adapter import RtspAdapter

_ADAPTERS: dict[str, type[DeviceAdapter]] = {
    "onvif": OnvifAdapter, "rtsp": RtspAdapter, "generic": OnvifAdapter,
}
_VENDOR_ALIASES: dict[str, str] = {}


def register_adapter(name: str, adapter: type[DeviceAdapter], *,
                     vendors: tuple[str, ...] = ()) -> None:
    key = name.strip().lower()
    if key in _ADAPTERS and _ADAPTERS[key] is not adapter:
        raise ValueError(f"adapter {name!r} is already registered")
    _ADAPTERS[key] = adapter
    for vendor in vendors:
        _VENDOR_ALIASES[vendor.strip().lower()] = key


def available_adapters() -> list[str]:
    return sorted(_ADAPTERS)


def resolve_adapter_name(*, adapter: str | None, protocol: str,
                         vendor: str | None) -> str:
    if adapter and adapter.lower() in _ADAPTERS and adapter.lower() != "generic":
        return adapter.lower()
    if vendor:
        alias = _VENDOR_ALIASES.get(vendor.strip().lower())
        if alias:
            return alias
    return "onvif" if protocol == "onvif" else "rtsp"


def build_adapter(name: str, endpoint: DeviceEndpoint, credentials: Credentials,
                  **kwargs) -> DeviceAdapter:
    adapter_cls = _ADAPTERS.get(name.lower())
    if adapter_cls is None:
        raise KeyError(f"unknown adapter {name!r}; available: {available_adapters()}")
    accepted = {"timeout_s"}
    if issubclass(adapter_cls, RtspAdapter):
        accepted |= {"main_path", "sub_path", "ffprobe_path"}
    if issubclass(adapter_cls, OnvifAdapter):
        accepted |= {"device_service_path"}
    return adapter_cls(endpoint, credentials,
                       **{k: v for k, v in kwargs.items() if k in accepted})
