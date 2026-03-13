"""DataUpdateCoordinator for SAJ eSolar integration."""
from datetime import datetime, timedelta
import logging
from typing import Any
import time

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import (
    DOMAIN,
    ENDPOINTS,
    UPDATE_INTERVAL,
    REGIONS,
    DEFAULT_APP_PROJECT_NAME,
    GREENHEISS_APP_PROJECT_NAME,
)
from .elekeeper import calc_signature, encrypt, generatkey

_LOGGER = logging.getLogger(__name__)

class SAJeSolarDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the SAJ eSolar API."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        region: str = "eu",
        monitored_plants: list[str] | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.session = session
        self.username = username
        self.password = password
        self.region = region
        self.base_url = REGIONS.get(region, REGIONS["eu"])
        if region not in REGIONS:
            _LOGGER.warning(
                "Unknown region '%s', falling back to 'eu' (%s)",
                region,
                self.base_url,
            )
        # Greenheiss is a reseller/OEM backend:
        # - It requires appProjectName "oem4Greenheiss" instead of SAJ "elekeeper".
        # - Its certificate chain is currently incomplete, so SSL verification must be disabled.
        if region == "gh":
            self.verify_ssl = False
            self.app_project_name = GREENHEISS_APP_PROJECT_NAME
        else:
            self.verify_ssl = True
            self.app_project_name = DEFAULT_APP_PROJECT_NAME
        _LOGGER.info(
            "Region '%s' using base URL %s with SSL verification %s and appProjectName '%s'",
            region,
            self.base_url,
            "enabled" if self.verify_ssl else "disabled",
            self.app_project_name,
        )
        self.monitored_plants = monitored_plants or []
        self.auth_token = None

    def _api_get(self, endpoint: str, **kwargs: Any):
        """API GET with integration-level SSL handling."""
        return self.session.get(
            f"{self.base_url}{endpoint}",
            ssl=self.verify_ssl,
            **kwargs,
        )

    def _api_post(self, endpoint: str, **kwargs: Any):
        """API POST with integration-level SSL handling."""
        return self.session.post(
            f"{self.base_url}{endpoint}",
            ssl=self.verify_ssl,
            **kwargs,
        )

    @staticmethod
    def _coerce_query_device_data_type(value: Any) -> int:
        """Normalize queryDeviceDataType values from API payloads."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    def _build_request_payload(
        self,
        request_data: dict[str, Any],
        *,
        timestamp_as_str: bool = False,
    ) -> dict[str, Any]:
        """Attach common request metadata and sign payload."""
        timestamp = int(time.time() * 1000)
        metadata: dict[str, Any] = {
            "appProjectName": self.app_project_name,
            "clientDate": datetime.now().strftime("%Y-%m-%d"),
            "lang": "en",
            "timeStamp": str(timestamp) if timestamp_as_str else timestamp,
            "random": generatkey(32),
            "clientId": "esolar-monitor-admin",
        }
        return calc_signature(request_data | metadata)

    def _build_query_context(
        self,
        plant_details: dict[str, Any],
        device_list: dict[str, Any],
    ) -> dict[str, Any]:
        """Build request context needed to query H1 and SEC plants."""
        # Different plant types require different identifier values in request params:
        # - H1 endpoints expect `deviceSn` (from device list `list[0].deviceSn`).
        # - SEC endpoints expect `emsSn` (from plant details `moduleSnList[0]` or moduleSn),
        #   selected when `queryDeviceDataType == 2`.
        # This centralized context avoids repeating that SEC vs H1 branching in every
        # endpoint method and also carries `office_id` for `searchOfficeIdArr` queries.
        plant_data = plant_details.get("data", {})
        devices = device_list.get("data", {}).get("list", [])
        device_data = devices[0] if devices else {}
        module_sn_list = plant_data.get("moduleSnList") or []

        ems_sn = module_sn_list[0] if module_sn_list else device_data.get("moduleSn")
        return {
            "query_device_data_type": self._coerce_query_device_data_type(
                plant_data.get("queryDeviceDataType")
            ),
            "device_sn": device_data.get("deviceSn"),
            "ems_sn": ems_sn,
            "office_id": str(plant_data.get("officeId") or "1"),
        }

    async def _get_sec_self_use_chart_data(
        self,
        plant_uid: str,
        query_context: dict[str, Any],
        chart_date_type: int,
    ) -> dict[str, Any]:
        """Get SEC self-use chart data for daily (1) or total (5) scope."""
        if query_context.get("query_device_data_type") != 2:
            return {}

        module_sn = query_context.get("ems_sn")
        if not module_sn:
            return {}

        chart_day = datetime.now().strftime("%Y-%m-%d")
        data: dict[str, Any] = {
            "plantUid": plant_uid,
            "moduleSn": module_sn,
            "chartDateType": chart_date_type,
            "chartDay": chart_day,
            "chartDayEnd": chart_day,
        }
        if chart_date_type == 1:
            data["customSearch"] = 1
        elif chart_date_type == 5:
            data["customSearch"] = 0

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["sec_self_use_chart"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get SEC self-use chart data: {resp.status}")
            return await resp.json()

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via API."""
        try:
            # Authenticate using new Elekeeper system
            await self._authenticate()

            # Get plant list
            plant_data = await self._get_plant_list()

            # Get data for each monitored plant
            plants_data = {}

            for plant_uid in self.monitored_plants:
                # Find the plant in the plant list
                plant_info = None
                for plant in plant_data.get("data", {}).get("list", []):
                    if plant["plantUid"] == plant_uid:
                        plant_info = plant
                        break

                if not plant_info:
                    _LOGGER.warning(f"Plant {plant_uid} not found in plant list")
                    continue

                # Get all data for this plant
                plant_details = await self._get_plant_details(plant_uid)
                query_context = self._build_query_context(plant_details, {"data": {"list": []}})
                device_list = await self._get_device_list(
                    plant_uid, query_context["office_id"]
                )
                query_context = self._build_query_context(plant_details, device_list)

                battery_list = await self._get_battery_list(
                    plant_uid, query_context["office_id"]
                )
                plant_statistics = await self._get_plant_statistics(
                    plant_uid, query_context
                )
                energy_flow = await self._get_energy_flow(
                    plant_uid, query_context
                )
                try:
                    self_use_daily = await self._get_sec_self_use_chart_data(
                        plant_uid,
                        query_context,
                        chart_date_type=1,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to fetch daily self-use data for plant %s: %s",
                        plant_uid,
                        err,
                    )
                    self_use_daily = {}
                try:
                    self_use_total = await self._get_sec_self_use_chart_data(
                        plant_uid,
                        query_context,
                        chart_date_type=5,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to fetch total self-use data for plant %s: %s",
                        plant_uid,
                        err,
                    )
                    self_use_total = {}

                # Get battery system info for this plant
                battery_info = await self._get_battery_info_for_plant(
                    plant_uid, query_context
                )

                # Get alarm information for this plant
                device_alarms = await self._get_device_alarms_for_plant(
                    plant_uid, query_context
                )

                plants_data[plant_uid] = {
                    "plant_info": plant_info,
                    "plant_details": plant_details,
                    "device_list": device_list,
                    "battery_list": battery_list,
                    "plant_statistics": plant_statistics,
                    "energy_flow": energy_flow,
                    "self_use_daily": self_use_daily,
                    "self_use_total": self_use_total,
                    "battery_info": battery_info,
                    "device_alarms": device_alarms,
                    "query_context": query_context,
                }

            return plants_data

        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}")
        except Exception as err:
            raise UpdateFailed(f"Error fetching data: {err}")

    async def _authenticate(self) -> None:
        """Authenticate with the SAJ eSolar API using Elekeeper method."""
        data_to_sign = self._build_request_payload({})

        login_data = {
            "username": self.username,
            "password": encrypt(self.password),
            "rememberMe": "false",
            "loginType": 1,
        }

        data = data_to_sign | login_data

        async with self._api_post(
            ENDPOINTS["login"],
            data=data,
        ) as resp:
            if resp.status == 401:
                raise ConfigEntryAuthFailed("Invalid authentication")
            if resp.status != 200:
                raise UpdateFailed(f"Login failed with status {resp.status}")

            response_data = await resp.json()

            if "errCode" in response_data and response_data["errCode"] != 0:
                raise ConfigEntryAuthFailed(f"Login failed: {response_data.get('errMsg', 'Unknown error')}")

            if "data" in response_data and "token" in response_data['data']:
                self.auth_token = response_data['data']['tokenHead'] + response_data['data']['token']
            else:
                raise UpdateFailed("Token not found in login response")

    async def _get_plant_list(self) -> dict[str, Any]:
        """Get list of plants."""
        data = {
            "pageNo": 1,
            "pageSize": 500,
        }

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["plant_list"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant list: {resp.status}")
            return await resp.json()

    async def _get_battery_info_for_plant(
        self,
        plant_uid: str,
        query_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get battery system info for specific plant."""
        if query_context is None:
            plant_details = await self._get_plant_details(plant_uid)
            initial_context = self._build_query_context(plant_details, {"data": {"list": []}})
            device_list = await self._get_device_list(
                plant_uid, initial_context["office_id"]
            )
            query_context = self._build_query_context(plant_details, device_list)

        device_sn = query_context.get("device_sn")
        if not device_sn:
            raise UpdateFailed(f"No device found for plant {plant_uid}")

        data = {
            "deviceSn": device_sn,
        }

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["battery_info"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get battery info: {resp.status}")
            return await resp.json()

    async def _get_device_alarms_for_plant(
        self,
        plant_uid: str,
        query_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get device alarms for specific plant."""
        if query_context is None:
            plant_details = await self._get_plant_details(plant_uid)
            initial_context = self._build_query_context(plant_details, {"data": {"list": []}})
            device_list = await self._get_device_list(
                plant_uid, initial_context["office_id"]
            )
            query_context = self._build_query_context(plant_details, device_list)

        device_sn = query_context.get("device_sn")
        if not device_sn:
            raise UpdateFailed(f"No device found for plant {plant_uid}")

        # Prepare form data for POST request
        form_data = {
            "deviceSn": device_sn,
            "orderByIndex": "1",
            "pageNo": "1",
            "pageSize": "10",
            "alarmCommonState": "1",
            "searchOfficeIdArr": query_context.get("office_id", "1"),
        }

        signed = self._build_request_payload(form_data, timestamp_as_str=True)

        async with self._api_post(
            ENDPOINTS["device_alarms"],
            data=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get device alarms: {resp.status}")
            return await resp.json()

    async def _get_plant_statistics(
        self,
        plant_uid: str,
        query_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get plant statistics for specific plant."""
        if query_context is None:
            plant_details = await self._get_plant_details(plant_uid)
            initial_context = self._build_query_context(plant_details, {"data": {"list": []}})
            device_list = await self._get_device_list(
                plant_uid, initial_context["office_id"]
            )
            query_context = self._build_query_context(plant_details, device_list)

        data = {
            "plantUid": plant_uid,
        }

        if query_context.get("query_device_data_type") == 2:
            ems_sn = query_context.get("ems_sn")
            if not ems_sn:
                raise UpdateFailed(f"No emsSn found for SEC plant {plant_uid}")
            data["emsSn"] = ems_sn
        else:
            device_sn = query_context.get("device_sn")
            if not device_sn:
                raise UpdateFailed(f"No device found for plant {plant_uid}")
            data["deviceSn"] = device_sn

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["plant_statistics"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant statistics: {resp.status}")
            return await resp.json()

    async def _get_energy_flow(
        self,
        plant_uid: str,
        query_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get energy flow for specific plant."""
        if query_context is None:
            plant_details = await self._get_plant_details(plant_uid)
            initial_context = self._build_query_context(plant_details, {"data": {"list": []}})
            device_list = await self._get_device_list(
                plant_uid, initial_context["office_id"]
            )
            query_context = self._build_query_context(plant_details, device_list)

        data = {
            "plantUid": plant_uid,
        }

        if query_context.get("query_device_data_type") == 2:
            ems_sn = query_context.get("ems_sn")
            if not ems_sn:
                raise UpdateFailed(f"No emsSn found for SEC plant {plant_uid}")
            data["emsSn"] = ems_sn
        else:
            device_sn = query_context.get("device_sn")
            if not device_sn:
                raise UpdateFailed(f"No device found for plant {plant_uid}")
            data["deviceSn"] = device_sn

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["energy_flow"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get energy flow: {resp.status}")
            return await resp.json()

    async def _get_plant_details(self, plant_uid: str) -> dict[str, Any]:
        """Get plant details for specific plant."""
        data = {
            "plantUid": plant_uid,
        }

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["plant_detail"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get plant details: {resp.status}")
            return await resp.json()

    async def _get_device_list(
        self,
        plant_uid: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Get device list for specific plant."""
        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": office_id or "1",
        }

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["device_list"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get device list: {resp.status}")
            return await resp.json()

    async def _get_battery_list(
        self,
        plant_uid: str,
        office_id: str | None = None,
    ) -> dict[str, Any]:
        """Get battery list for specific plant."""
        data = {
            "plantUid": plant_uid,
            "pageSize": 100,
            "pageNo": 1,
            "searchOfficeIdArr": office_id or "1",
        }

        signed = self._build_request_payload(data)

        async with self._api_get(
            ENDPOINTS["battery_list"],
            params=signed,
            headers={'Authorization': self.auth_token},
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"Failed to get battery list: {resp.status}")
            return await resp.json()
