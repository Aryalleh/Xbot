import asyncio
import websockets

async def main():
    uri = "ws://127.0.0.1:8765"
    print("Connecting to", uri, flush=True)

    try:
        async with websockets.connect(uri) as ws:
            print("Connected.", flush=True)
            async for msg in ws:
                print("RECV:", msg, flush=True)
    except Exception as e:
        print("ERROR:", repr(e), flush=True)

asyncio.run(main())