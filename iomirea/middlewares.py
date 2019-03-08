import asyncio

from aiohttp import web

from log import server_log
from utils import converters


@web.middleware
async def error_handler(req, handler):
    try:
        return await handler(req)
    except (web.HTTPSuccessful, web.HTTPRedirection):
        raise
    except web.HTTPException as e:
        status = e.status
        message = e.text
    except (web.HTTPMethodNotAllowed, asyncio.CancelledError):
        if req.config_dict["args"].debug:
            raise

        status = 500
        message = f"{status} Internal server error"
    except Exception as e:
        server_log.exception(
            "Error handling request", exc_info=e, extra={"request": req}
        )
        status = 500
        message = f"{status}: Internal server error"

    return web.json_response({"message": message}, status=status)


@web.middleware
async def match_info_validator(req, handler):
    req["match_info"] = {}

    for key, value in req.match_info.items():
        if not key.endswith("_id"):
            continue

        try:
            req["match_info"][key] = await converters.ID().convert(value, req.app)
        except (converters.ConvertError, converters.CheckError) as e:
            server_log.debug(f"match_validator: {value}: {e}")
            raise web.HTTPNotFound

    return await handler(req)
