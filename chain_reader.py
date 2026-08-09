"""
وحدة القراءة المباشرة من البلوكشين مع سيرفرات RPC احتياطية سريعة لشبكة روبن هود وإيثيريوم.
"""

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
import aiohttp
import requests
from eth_abi import decode as eth_abi_decode, encode as eth_abi_encode
from web3 import Web3

log = logging.getLogger("chain-reader")

ALCHEMY_ETH_KEY = os.environ.get("ALCHEMY_ETH_KEY") or os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_ROBINHOOD_KEY = os.environ.get("ALCHEMY_ROBINHOOD_KEY", ALCHEMY_ETH_KEY)

# سيرفرات سريعة احتياطية لشبكة روبن هود وإيثيريوم
RPC_URLS = {
    "ethereum": [
        f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}" if ALCHEMY_ETH_KEY else None,
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
    ],
    "mainnet": [
        f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}" if ALCHEMY_ETH_KEY else None,
        "https://eth.llamarpc.com",
    ],
    "robinhood": [
        f"https://robinhood-mainnet.g.alchemy.com/v2/{ALCHEMY_ROBINHOOD_KEY}" if ALCHEMY_ROBINHOOD_KEY else None,
        "https://robinhood-mainnet.g.alchemy.com/v2/demo",
    ],
}

MULTICALL3_ADDRESS = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
TOKEN_URI_SELECTOR = bytes.fromhex("c87b56dd")
MULTICALL3_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

PINATA_GATEWAY_DOMAIN = os.environ.get("PINATA_GATEWAY_DOMAIN", "").strip()
PINATA_GATEWAY_KEY = os.environ.get("PINATA_GATEWAY_KEY", "").strip()

IPFS_GATEWAYS_NAMED = [
    ("pinata-public", "https://gateway.pinata.cloud/ipfs/"),
    ("ipfs.io", "https://ipfs.io/ipfs/"),
    ("cloudflare", "https://cloudflare-ipfs.com/ipfs/"),
]


def _pinata_dedicated_url(path: str) -> str | None:
    if not PINATA_GATEWAY_DOMAIN or not PINATA_GATEWAY_KEY:
        return None
    return f"https://{PINATA_GATEWAY_DOMAIN}/ipfs/{path}?pinataGatewayToken={PINATA_GATEWAY_KEY}"


_w3_instances = {}


def get_web3(chain: str = "ethereum") -> Web3:
    chain_key = (chain or "ethereum").lower().strip()
    if chain_key in ("mainnet", "eth"):
        chain_key = "ethereum"

    if chain_key not in _w3_instances:
        urls = RPC_URLS.get(chain_key, RPC_URLS["ethereum"])
        valid_rpc = next((u for u in urls if u), RPC_URLS["ethereum"][0])
        _w3_instances[chain_key] = Web3(Web3.HTTPProvider(valid_rpc, request_kwargs={"timeout": 6}))
    return _w3_instances[chain_key]


def build_token_uri_calldata(token_id: int) -> bytes:
    return TOKEN_URI_SELECTOR + eth_abi_encode(["uint256"], [token_id])


async def async_batch_get_token_uris(contract_address: str, token_ids: list[int], chain: str = "ethereum") -> dict:
    chain_key = (chain or "ethereum").lower().strip()
    if chain_key in ("mainnet", "eth"):
        chain_key = "ethereum"

    urls = [u for u in RPC_URLS.get(chain_key, RPC_URLS["ethereum"]) if u]
    contract_checksum = Web3.to_checksum_address(contract_address)

    calls = [
        (contract_checksum, True, build_token_uri_calldata(token_id))
        for token_id in token_ids
    ]

    multicall_calldata = "0x" + (
        MULTICALL3_AGGREGATE3_SELECTOR + eth_abi_encode(["(address,bool,bytes)[]"], [calls])
    ).hex()

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": MULTICALL3_ADDRESS, "data": multicall_calldata},
            "latest"
        ],
        "id": 1
    }

    output = {tid: None for tid in token_ids}
    headers = {"Content-Type": "application/json"}

    # تجربة السيرفر الرئيسي ثم الاحتياطي مهلة 3.5 ثانية فقط لعدم التعليق
    for rpc_url in urls:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=3.5)) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        raw_hex = res_json.get("result", "")
                        if raw_hex and raw_hex.startswith("0x"):
                            return_bytes = bytes.fromhex(raw_hex[2:])
                            (results,) = eth_abi_decode(["(bool,bytes)[]"], return_bytes)
                            for token_id, (success, return_data) in zip(token_ids, results):
                                if success and return_data:
                                    try:
                                        (decoded_uri,) = eth_abi_decode(["string"], return_data)
                                        output[token_id] = decoded_uri
                                    except Exception:
                                        pass
                            return output
        except Exception:
            continue

    return output


def batch_get_token_uris(contract_address: str, token_ids: list[int], chain: str = "ethereum") -> dict:
    try:
        return asyncio.run(async_batch_get_token_uris(contract_address, token_ids, chain))
    except Exception:
        return {tid: None for tid in token_ids}


def detect_global_reveal_flag(contract_address: str, chain: str = "ethereum") -> bool | None:
    try:
        w3 = get_web3(chain)
        checksum_addr = Web3.to_checksum_address(contract_address)
        selectors = [
            bytes.fromhex("66c8913d"),
            bytes.fromhex("f209c13e"),
        ]
        for sel in selectors:
            try:
                res = w3.eth.call({"to": checksum_addr, "data": sel})
                if res and len(res) == 32:
                    (is_rev,) = eth_abi_decode(["bool"], res)
                    return bool(is_rev)
            except Exception:
                continue
        return None
    except Exception:
        return None


async def _fetch_gw_timed(session: aiohttp.ClientSession, name: str, url: str) -> tuple:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
    start = time.monotonic()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            elapsed = time.monotonic() - start
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return (name, data, elapsed)
            return (name, None, elapsed)
    except Exception:
        return (name, None, time.monotonic() - start)


async def _race_gateways(session: aiohttp.ClientSession, contenders: list[tuple]) -> dict | None:
    tasks = {asyncio.create_task(_fetch_gw_timed(session, name, url)): name for name, url in contenders}
    pending = set(tasks.keys())
    winner = None

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name, data, elapsed = task.result()
            if data and winner is None:
                winner = (name, data, elapsed)

        if winner:
            for t in pending:
                t.cancel()
            break

    return winner[1] if winner else None


async def _async_fetch_single_metadata(session: aiohttp.ClientSession, uri: str) -> dict | None:
    if not uri:
        return None

    try:
        if uri.startswith("data:application/json;base64,"):
            payload = uri.split(",", 1)[1]
            return json.loads(base64.b64decode(payload))

        if uri.startswith("data:application/json,"):
            payload = uri.split(",", 1)[1]
            return json.loads(urllib.parse.unquote(payload))

        if uri.startswith("ipfs://"):
            path = uri[len("ipfs://"):]
            if path.startswith("ipfs/"):
                path = path[5:]

            contenders = [(name, base + path) for name, base in IPFS_GATEWAYS_NAMED]
            dedicated_url = _pinata_dedicated_url(path)
            if dedicated_url:
                contenders = [("pinata-dedicated", dedicated_url)] + contenders

            return await _race_gateways(session, contenders)

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
        async with session.get(uri, headers=headers, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
        return None
    except Exception:
        return None


async def async_batch_resolve_metadata(uri_map: dict[int, str]) -> dict[int, dict | None]:
    connector = aiohttp.TCPConnector(limit=150, ttl_dns_cache=300, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _async_fetch_single_metadata(session, uri)
            for tid, uri in uri_map.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        metadata_dict = {}
        for (tid, uri), res in zip(uri_map.items(), results):
            if isinstance(res, dict):
                metadata_dict[tid] = res
            else:
                metadata_dict[tid] = None

        return metadata_dict


def resolve_metadata(uri: str) -> dict | None:
    if not uri:
        return None

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
    try:
        if uri.startswith("data:application/json;base64,"):
            payload = uri.split(",", 1)[1]
            return json.loads(base64.b64decode(payload))

        if uri.startswith("data:application/json,"):
            payload = uri.split(",", 1)[1]
            return json.loads(urllib.parse.unquote(payload))

        if uri.startswith("ipfs://"):
            path = uri[len("ipfs://"):]
            if path.startswith("ipfs/"):
                path = path[5:]

            gateways_to_try = [(name, base + path) for name, base in IPFS_GATEWAYS_NAMED]
            dedicated_url = _pinata_dedicated_url(path)
            if dedicated_url:
                gateways_to_try = [("pinata-dedicated", dedicated_url)] + gateways_to_try

            for name, url in gateways_to_try:
                try:
                    resp = requests.get(url, headers=headers, timeout=2.5)
                    if resp.status_code == 200:
                        return resp.json()
                except Exception:
                    continue
            return None

        resp = requests.get(uri, headers=headers, timeout=3)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None
