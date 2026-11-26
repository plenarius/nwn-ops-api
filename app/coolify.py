import logging

import aiohttp

logger = logging.getLogger(__name__)


class CoolifyClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession):
        self._base_url = base_url
        self._token = token
        self._session = session

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def post(self, path: str) -> dict:
        url = f"{self._base_url}/api/v1{path}"
        async with self._session.post(url, headers=self._headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_envs(self, service_uuid: str) -> list[dict]:
        url = f"{self._base_url}/api/v1/services/{service_uuid}/envs"
        async with self._session.get(url, headers=self._headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def patch_env(self, service_uuid: str, key: str, value: str) -> None:
        envs = await self.get_envs(service_uuid)
        env_uuid = next((e["uuid"] for e in envs if e["key"] == key), None)
        if not env_uuid:
            logger.error("Coolify env var %s not found", key)
            return
        url = f"{self._base_url}/api/v1/services/{service_uuid}/envs"
        async with self._session.patch(
            url,
            headers=self._headers,
            json={"uuid": env_uuid, "key": key, "value": value},
        ) as resp:
            # Coolify answers 200 or 201 (Created) on success. Its body echoes the variable's value, so it is never
            # logged, nor is the value: some of these variables are secrets.
            if 200 <= resp.status < 300:
                logger.info("Updated Coolify env %s", key)
            else:
                logger.error("Failed to update Coolify env %s: HTTP %s", key, resp.status)

    async def get_service(self, service_uuid: str) -> dict:
        url = f"{self._base_url}/api/v1/services/{service_uuid}"
        async with self._session.get(url, headers=self._headers) as resp:
            resp.raise_for_status()
            return await resp.json()
