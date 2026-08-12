"""
سكربت تشخيصي موجه - يقرأ البيانات الخام من البلوكشين و IPFS مباشرة للقطع حول الحد الفاصل.
"""

import asyncio
import json
import os
import aiohttp
from eth_abi import decode as eth_abi_decode, encode as eth_abi_encode
from web3 import Web3

ALCHEMY_ETH_KEY = os.environ.get("ALCHEMY_ETH_KEY") or os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_ROBINHOOD_KEY = os.environ.get("ALCHEMY_ROBINHOOD_KEY", ALCHEMY_ETH_KEY)

RPC_URLS = {
    "ethereum": f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}",
    "mainnet": f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}",
    "robinhood": f"https://robinhood-mainnet.g.alchemy.com/v2/{ALCHEMY_ROBINHOOD_KEY}",
}

TOKEN_URI_SELECTOR = bytes.fromhex("c87b56dd")  # tokenURI(uint256)

IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://ipfs.filebase.io/ipfs/",
]


def build_token_uri_calldata(token_id: int) -> str:
    return "0x" + (TOKEN_URI_SELECTOR + eth_abi_encode(["uint256"], [token_id])).hex()


async def direct_eth_call(rpc_url: str, contract_address: str, calldata: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_address, "data": calldata}, "latest"],
        "id": 1
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                res_json = await resp.json()
                if "error" in res_json:
                    return f"REVERT_ERROR: {res_json['error'].get('message', 'Unknown Error')}"
                return res_json.get("result")
    except Exception as e:
        return f"RPC_FAIL: {e}"


async def diagnose_token_id(contract_address: str, token_id: int, chain: str = "robinhood"):
    rpc_url = RPC_URLS.get(chain, RPC_URLS["ethereum"])
    calldata = build_token_uri_calldata(token_id)
    
    print(f"\n--- [فحص القطعة #{token_id}] ---")
    raw_hex = await direct_eth_call(rpc_url, contract_address, calldata)
    
    if not raw_hex or raw_hex.startswith("RPC_FAIL") or raw_hex.startswith("REVERT_ERROR"):
        print(f"❌ استجابة العقد على البلوكشين: {raw_hex}")
        return

    try:
        raw_bytes = bytes.fromhex(raw_hex[2:])
        (token_uri,) = eth_abi_decode(["string"], raw_bytes)
        print(f"✅ tokenURI الصريح من البلوكشين: {token_uri}")
        
        # اختبار جلب الميتاداتا
        if token_uri.startswith("ipfs://"):
            path = token_uri[7:]
            if path.startswith("ipfs/"):
                path = path[5:]
            
            print(f"🔍 تجربة جلب IPFS مسار: {path}")
            async with aiohttp.ClientSession() as session:
                for gw in IPFS_GATEWAYS:
                    url = gw + path
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as gw_resp:
                            print(f"   - بوابة [{gw.split('/')[2]}]: HTTP Status {gw_resp.status}")
                            if gw_resp.status == 200:
                                meta = await gw_resp.json(content_type=None)
                                print(f"     ✅ نجاح! اسم القطعة: '{meta.get('name')}' | عدد الصفات: {len(meta.get('traits', meta.get('attributes', [])))}")
                                break
                    except Exception as e:
                        print(f"   - بوابة [{gw.split('/')[2]}]: فشل الاتصال ({e})")
        else:
            print(f"🔗 الرابط ليس IPFS تقليدي (نوع آخر أو HTTP/Base64)")
            
    except Exception as e:
        print(f"⚠️ فشل فك شفرة الاستجابة ({e}) | Hex: {raw_hex[:50]}...")


async def run_diagnostics(contract_address: str, chain: str = "robinhood"):
    print(f"==================================================")
    print(f"🚀 بدء التشخيص المباشر للعقد: {contract_address} على شبكة ({chain})")
    print(f"==================================================")
    
    # فحص المعروض الكلي اللحظي
    w3 = Web3(Web3.HTTPProvider(RPC_URLS.get(chain, RPC_URLS["ethereum"])))
    checksum_addr = Web3.to_checksum_address(contract_address)
    
    try:
        total_sp_hex = w3.eth.call({"to": checksum_addr, "data": bytes.fromhex("18160ddd")})
        (total_sp,) = eth_abi_decode(["uint256"], total_sp_hex)
        print(f"📊 On-Chain totalSupply(): {total_sp}")
    except Exception as e:
        print(f"⚠️ تعذر قراءة totalSupply(): {e}")

    try:
        max_sp_hex = w3.eth.call({"to": checksum_addr, "data": bytes.fromhex("d5abeb01")})
        (max_sp,) = eth_abi_decode(["uint256"], max_sp_hex)
        print(f"📊 On-Chain maxSupply(): {max_sp}")
    except Exception as e:
        print(f"⚠️ تعذر قراءة maxSupply(): {e}")

    # فحص التوكين 0
    await diagnose_token_id(contract_address, 0, chain)
    
    # فحص القطع حول الحد الفاصل (7461، 7462، 7463، 7464، 7771، 7772)
    test_ids = [7461, 7462, 7463, 7464, 7465, 7771, 7772]
    for tid in test_ids:
        await diagnose_token_id(contract_address, tid, chain)


if __name__ == "__main__":
    # ضع عنوان عقد المجموعة المراد تشخيصها هنا
    # مثال كولكشن Motion على Robinhood
    TARGET_CONTRACT = "0x..."  # ضع عنوان عقد المجموعة هنا
    TARGET_CHAIN = "robinhood"  # أو "ethereum"
    
    asyncio.run(run_diagnostics(TARGET_CONTRACT, TARGET_CHAIN))"""
سكربت تشخيصي موجه - يقرأ البيانات الخام من البلوكشين و IPFS مباشرة للقطع حول الحد الفاصل.
"""

import asyncio
import json
import os
import aiohttp
from eth_abi import decode as eth_abi_decode, encode as eth_abi_encode
from web3 import Web3

ALCHEMY_ETH_KEY = os.environ.get("ALCHEMY_ETH_KEY") or os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_ROBINHOOD_KEY = os.environ.get("ALCHEMY_ROBINHOOD_KEY", ALCHEMY_ETH_KEY)

RPC_URLS = {
    "ethereum": f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}",
    "mainnet": f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_ETH_KEY}",
    "robinhood": f"https://robinhood-mainnet.g.alchemy.com/v2/{ALCHEMY_ROBINHOOD_KEY}",
}

TOKEN_URI_SELECTOR = bytes.fromhex("c87b56dd")  # tokenURI(uint256)

IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://ipfs.filebase.io/ipfs/",
]


def build_token_uri_calldata(token_id: int) -> str:
    return "0x" + (TOKEN_URI_SELECTOR + eth_abi_encode(["uint256"], [token_id])).hex()


async def direct_eth_call(rpc_url: str, contract_address: str, calldata: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_address, "data": calldata}, "latest"],
        "id": 1
    }
    headers = {"Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                res_json = await resp.json()
                if "error" in res_json:
                    return f"REVERT_ERROR: {res_json['error'].get('message', 'Unknown Error')}"
                return res_json.get("result")
    except Exception as e:
        return f"RPC_FAIL: {e}"


async def diagnose_token_id(contract_address: str, token_id: int, chain: str = "robinhood"):
    rpc_url = RPC_URLS.get(chain, RPC_URLS["ethereum"])
    calldata = build_token_uri_calldata(token_id)
    
    print(f"\n--- [فحص القطعة #{token_id}] ---")
    raw_hex = await direct_eth_call(rpc_url, contract_address, calldata)
    
    if not raw_hex or raw_hex.startswith("RPC_FAIL") or raw_hex.startswith("REVERT_ERROR"):
        print(f"❌ استجابة العقد على البلوكشين: {raw_hex}")
        return

    try:
        raw_bytes = bytes.fromhex(raw_hex[2:])
        (token_uri,) = eth_abi_decode(["string"], raw_bytes)
        print(f"✅ tokenURI الصريح من البلوكشين: {token_uri}")
        
        # اختبار جلب الميتاداتا
        if token_uri.startswith("ipfs://"):
            path = token_uri[7:]
            if path.startswith("ipfs/"):
                path = path[5:]
            
            print(f"🔍 تجربة جلب IPFS مسار: {path}")
            async with aiohttp.ClientSession() as session:
                for gw in IPFS_GATEWAYS:
                    url = gw + path
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as gw_resp:
                            print(f"   - بوابة [{gw.split('/')[2]}]: HTTP Status {gw_resp.status}")
                            if gw_resp.status == 200:
                                meta = await gw_resp.json(content_type=None)
                                print(f"     ✅ نجاح! اسم القطعة: '{meta.get('name')}' | عدد الصفات: {len(meta.get('traits', meta.get('attributes', [])))}")
                                break
                    except Exception as e:
                        print(f"   - بوابة [{gw.split('/')[2]}]: فشل الاتصال ({e})")
        else:
            print(f"🔗 الرابط ليس IPFS تقليدي (نوع آخر أو HTTP/Base64)")
            
    except Exception as e:
        print(f"⚠️ فشل فك شفرة الاستجابة ({e}) | Hex: {raw_hex[:50]}...")


async def run_diagnostics(contract_address: str, chain: str = "robinhood"):
    print(f"==================================================")
    print(f"🚀 بدء التشخيص المباشر للعقد: {contract_address} على شبكة ({chain})")
    print(f"==================================================")
    
    # فحص المعروض الكلي اللحظي
    w3 = Web3(Web3.HTTPProvider(RPC_URLS.get(chain, RPC_URLS["ethereum"])))
    checksum_addr = Web3.to_checksum_address(contract_address)
    
    try:
        total_sp_hex = w3.eth.call({"to": checksum_addr, "data": bytes.fromhex("18160ddd")})
        (total_sp,) = eth_abi_decode(["uint256"], total_sp_hex)
        print(f"📊 On-Chain totalSupply(): {total_sp}")
    except Exception as e:
        print(f"⚠️ تعذر قراءة totalSupply(): {e}")

    try:
        max_sp_hex = w3.eth.call({"to": checksum_addr, "data": bytes.fromhex("d5abeb01")})
        (max_sp,) = eth_abi_decode(["uint256"], max_sp_hex)
        print(f"📊 On-Chain maxSupply(): {max_sp}")
    except Exception as e:
        print(f"⚠️ تعذر قراءة maxSupply(): {e}")

    # فحص التوكين 0
    await diagnose_token_id(contract_address, 0, chain)
    
    # فحص القطع حول الحد الفاصل (7461، 7462، 7463، 7464، 7771، 7772)
    test_ids = [7461, 7462, 7463, 7464, 7465, 7771, 7772]
    for tid in test_ids:
        await diagnose_token_id(contract_address, tid, chain)


if __name__ == "__main__":
    # ضع عنوان عقد المجموعة المراد تشخيصها هنا
    # مثال كولكشن Motion على Robinhood
    TARGET_CONTRACT = "0x780f5753fe7d66ea135c065a975e578f25c145bd"  # ضع عنوان عقد المجموعة هنا
    TARGET_CHAIN = "robinhood"  # أو "ethereum"
    
    asyncio.run(run_diagnostics(TARGET_CONTRACT, TARGET_CHAIN))
