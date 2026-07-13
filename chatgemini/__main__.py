"""Command-line entry point for ChatGemini."""
import argparse
import os

from . import __version__
from .config import CONFIG, find_config, load_config
from .models import MODELS
from .server import ChatHandler, ThreadedServer


def main():
    parser = argparse.ArgumentParser(description="ChatGemini OpenAI-compatible chat gateway")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--version", action="version", version=f"ChatGemini {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("CHATGEMINI_CONFIG") or find_config()
    if config_path:
        load_config(config_path)
    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    server = ThreadedServer((CONFIG["host"], CONFIG["port"]), ChatHandler)
    print(f"ChatGemini v{__version__}")
    print(f"  Listening: http://{CONFIG['host']}:{CONFIG['port']}")
    print(f"  OpenAI:   http://127.0.0.1:{CONFIG['port']}/v1")
    print(f"  Models:   {', '.join(MODELS)}")
    print(f"  Cookie:   {'configured' if CONFIG.get('cookie_file') else 'anonymous'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
