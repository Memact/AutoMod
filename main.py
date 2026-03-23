from bot import MemactAutoModBot
from config import load_settings
from utils.keepalive import KeepAliveState, start_keepalive_server


def main() -> None:
    settings = load_settings()
    keepalive_state = KeepAliveState()
    keepalive_server = start_keepalive_server(keepalive_state)
    bot = MemactAutoModBot(settings)
    bot.keepalive_state = keepalive_state
    keepalive_state.set_status("connecting", "Connecting to Discord.")

    try:
        bot.run(settings.token)
    except Exception as error:
        keepalive_state.set_status("crashed", f"{type(error).__name__}: {error}")
        if keepalive_server is not None:
            keepalive_server.shutdown()
            keepalive_server.server_close()
        raise


if __name__ == "__main__":
    main()
