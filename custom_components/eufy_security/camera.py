from __future__ import annotations

import asyncio
import contextlib
import logging

from haffmpeg.camera import CameraMjpeg

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COORDINATOR, DOMAIN, Schema
from .coordinator import EufySecurityDataUpdateCoordinator
from .entity import EufySecurityEntity
from .eufy_security_api.camera import StreamProvider, StreamStatus
from .eufy_security_api.metadata import Metadata
from .eufy_security_api.util import wait_for_value_to_equal

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Setup camera entities."""
    coordinator: EufySecurityDataUpdateCoordinator = hass.data[DOMAIN][COORDINATOR]
    product_properties = []
    for product in coordinator.api.devices.values():
        if product.is_camera is True:
            product_properties.append(Metadata.parse(product, {"name": "camera", "label": "Camera"}))

    entities = [EufySecurityCamera(coordinator, metadata) for metadata in product_properties]
    async_add_entities(entities)

    # register entity level services
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("start_p2p_livestream", {}, "_start_livestream")
    platform.async_register_entity_service("stop_p2p_livestream", {}, "_stop_livestream")
    platform.async_register_entity_service("start_rtsp_livestream", {}, "_start_rtsp_livestream")
    platform.async_register_entity_service("stop_rtsp_livestream", {}, "_stop_rtsp_livestream")
    platform.async_register_entity_service("ptz_up", {}, "_async_ptz_up")
    platform.async_register_entity_service("ptz_down", {}, "_async_ptz_down")
    platform.async_register_entity_service("ptz_left", {}, "_async_ptz_left")
    platform.async_register_entity_service("ptz_right", {}, "_async_ptz_right")
    platform.async_register_entity_service("ptz_360", {}, "_async_ptz_360")

    platform.async_register_entity_service(
        "trigger_camera_alarm_with_duration", Schema.TRIGGER_ALARM_SERVICE_SCHEMA.value, "_async_alarm_trigger"
    )
    platform.async_register_entity_service("reset_alarm", {}, "_async_reset_alarm")
    platform.async_register_entity_service("quick_response", Schema.QUICK_RESPONSE_SERVICE_SCHEMA.value, "_async_quick_response")
    platform.async_register_entity_service("snooze", Schema.SNOOZE.value, "_snooze")


class EufySecurityCamera(Camera, EufySecurityEntity):
    """Base camera entity for integration"""

    def __init__(self, coordinator: EufySecurityDataUpdateCoordinator, metadata: Metadata) -> None:
        Camera.__init__(self)
        EufySecurityEntity.__init__(self, coordinator, metadata)
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_name = f"{self.product.name}"

        # camera image
        self._last_url = None
        self._last_image = None

        # ffmpeg entities
        self.ffmpeg = self.coordinator.hass.data[DATA_FFMPEG]
        self.product.set_ffmpeg(CameraMjpeg(self.ffmpeg.binary))

    async def stream_source(self) -> str:
        if self.is_streaming is False:
            return None
        return self.product.stream_url

    async def handle_async_mjpeg_stream(self, request):
        stream_source = await self.stream_source()
        if stream_source is None:
            return await super().handle_async_mjpeg_stream(request)
        stream = CameraMjpeg(self.ffmpeg.binary)
        await stream.open_camera(stream_source)
        try:
            return await async_aiohttp_proxy_stream(self.hass, request, await stream.get_reader(), self.ffmpeg.ffmpeg_stream_content_type)
        finally:
            await stream.close()

    @property
    def available(self) -> bool:
        return True

    async def async_create_stream(self):
        if self.coordinator.config.no_stream_in_hass is True:
            return None
        return await super().async_create_stream()

    async def _start_hass_streaming(self):
        await wait_for_value_to_equal(self.product.__dict__, "stream_status", StreamStatus.STREAMING)
        await self._stop_hass_streaming()
        if await self.async_create_stream() is None:
            return
        await self.stream.start()
        await self.async_camera_image()

    async def _stop_hass_streaming(self):
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None

    @property
    def is_streaming(self) -> bool:
        """Return true if the device is recording."""
        return self.product.stream_status == StreamStatus.STREAMING

    async def _get_image_from_hass_stream(self, width, height):
        while True:
            result = await self.stream.async_get_image(width, height)
            if result is not None:
                return result

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        _LOGGER.debug(f"image 1 - {self.is_streaming} - {self.stream}")
        if self.is_streaming is True:
            if self.stream is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    self._last_image = await asyncio.wait_for(self._get_image_from_hass_stream(width, height), 5)
                _LOGGER.debug(f"image 3.1 - {self.is_streaming} - is_empty  {self._last_image is None} - {self.stream.available}")
            else:
                self._last_image = await ffmpeg.async_get_image(self.hass, await self.stream_source(), width=width, height=height)
                _LOGGER.debug(f"image 3.2 - {self.is_streaming} - is_empty  {self._last_image is None}")
            self._last_url = None

        # else:
        #     current_url = get_child_value(self.product.properties, MessageField.PICTURE_URL.value)
        #     if current_url != self._last_url and current_url.startswith("https"):
        #         async with async_get_clientsession(self.coordinator.hass).get(current_url) as response:
        #             if response.status == 200:
        #                 self._last_image = await response.read()
        #                 self._last_url = current_url
        #                 _LOGGER.debug(f"async_camera_image 4 - is_empty {self._last_image is None}")

        _LOGGER.debug(f"async_camera_image 5 - is_empty {self._last_image is None}")
        if self._last_image is not None:
            _LOGGER.debug(f"async_camera_image 6 - {len(self._last_image)}")
        return self._last_image

    async def _start_livestream(self) -> None:
        """start byte based livestream on camera"""
        if await self.product.start_livestream() is False:
            await self._stop_livestream()
        else:
            await self._start_hass_streaming()

    async def _stop_livestream(self) -> None:
        """stop byte based livestream on camera"""
        await self._stop_hass_streaming()
        await self.product.stop_livestream()

    async def _start_rtsp_livestream(self) -> None:
        """start rtsp based livestream on camera"""
        if await self.product.start_rtsp_livestream() is False:
            await self._stop_rtsp_livestream()
        else:
            await self._start_hass_streaming()

    async def _stop_rtsp_livestream(self) -> None:
        """stop rtsp based livestream on camera"""
        await self._stop_hass_streaming()
        await self.product.stop_rtsp_livestream()

    async def _async_alarm_trigger(self, duration: int = 10):
        """trigger alarm for a duration on camera"""
        await self.product.trigger_alarm(duration)

    async def _async_reset_alarm(self) -> None:
        """reset ongoing alarm"""
        await self.product.reset_alarm()

    async def async_turn_on(self) -> None:
        """Turn off camera."""
        if self.product.stream_provider == StreamProvider.RTSP:
            await self._start_rtsp_livestream()
        else:
            await self._start_livestream()

    async def async_turn_off(self) -> None:
        """Turn off camera."""
        if self.product.stream_provider == StreamProvider.RTSP:
            await self._stop_rtsp_livestream()
        else:
            await self._stop_livestream()

    async def _async_ptz_up(self) -> None:
        await self.product.ptz_up()

    async def _async_ptz_down(self) -> None:
        await self.product.ptz_down()

    async def _async_ptz_left(self) -> None:
        await self.product.ptz_left()

    async def _async_ptz_right(self) -> None:
        await self.product.ptz_right()

    async def _async_ptz_360(self) -> None:
        await self.product.ptz_360()

    async def _async_quick_response(self, voice_id: int) -> None:
        await self.product.quick_response(voice_id)

    async def _snooze(self, snooze_time: int, snooze_chime: bool, snooze_motion: bool, snooze_homebase: bool) -> None:
        await self.product.snooze(snooze_time, snooze_chime, snooze_motion, snooze_homebase)
