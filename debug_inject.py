import argparse
import asyncio
import time

import main
from message_store import IncomingMessage


async def inject_message(content: str, sender: str, attachment: str | None) -> None:
    sent_messages: list[str] = []

    async def fake_send(message: str, recipient: str, logger) -> None:
        sent_messages.append(message)
        print(f"[send_imessage -> {recipient}]")
        print(message)
        print("---")

    original_send = main.send_imessage
    original_queue = main.task_queue
    try:
        main.send_imessage = fake_send
        main.task_queue = asyncio.Queue()

        worker = asyncio.create_task(main.queue_worker())
        message = IncomingMessage(
            rowid=int(time.time()),
            text=content,
            date=int(time.time()),
            attachment=attachment,
            sender=sender,
        )
        await main.handle_incoming_message(message)
        await asyncio.sleep(0.1)
        await main.task_queue.join()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        if not sent_messages:
            print("[no outgoing messages]")
    finally:
        main.send_imessage = original_send
        main.task_queue = original_queue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inject a synthetic inbound bridge message for local debugging.")
    parser.add_argument("content", help="Synthetic inbound message content")
    parser.add_argument("--sender", default="debug@local", help="Synthetic sender id")
    parser.add_argument("--attachment", default=None, help="Optional attachment path")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(inject_message(args.content, args.sender, args.attachment))
