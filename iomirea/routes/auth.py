"""
IOMirea-server - A server for IOMirea messenger
Copyright (C) 2019  Eugene Ershov

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""


import re
import uuid

from typing import Dict, Any, Union

import bcrypt
import aiohttp_jinja2
import aiohttp_session

from aiohttp import web

from utils import helpers, smtp
from models import converters, checks
from models.confirmation_codes import EmailConfirmationCode, PasswordResetCode
from errors import ConvertError
from db.redis import REMOVE_EXPIRED_COOKIES
from security.security_checks import check_user_password
from constants import ContentType


class Email(converters.Converter):
    EMAIL_REGEX = re.compile(r"[a-z0-9_.+-]+@[a-z0-9-]+\.[a-z0-9-.]+")

    async def _convert(self, value: str, app: web.Application) -> str:
        value = value.lower()

        if self.EMAIL_REGEX.fullmatch(value) is None:
            raise ConvertError("Bad email pattern")

        return value


async def send_email_confirmation_code(
    email: str, code: str, req: web.Request
) -> None:
    relative_url = (
        req.app.router["confirm_email"].url_for().with_query({"code": code})
    )
    url = req.url.join(relative_url)

    text = (
        f"Subject: IOMirea registration confirmation\n\n"
        f"This email was used to register on IOMirea service.\n"
        f"If you did not do it, please, ignore this message.\n\n"
        f"To finish registration, use this url: {url}"
    )

    await smtp.send_message([email], text, req.config_dict["config"])


async def send_password_reset_code(
    email: str, code: str, user_name: str, req: web.Request
) -> None:
    relative_url = (
        req.app.router["reset_password"].url_for().with_query({"code": code})
    )

    url = req.url.join(relative_url)

    text = (
        f"Subject: IOMirea password reset\n\n"
        f"Password reset requested for account {user_name}\n"
        f"Use this url to reset your password: {url}\n"
        f"If you did not request reset, ignore this email"
    )

    await smtp.send_message([email], text, req.config_dict["config"])


async def new_session(
    req: web.Request, user_id: int, *, clean_cookies: bool = False
) -> None:
    """Generates and saves new aiohttp_session session."""

    session = await aiohttp_session.new_session(req)
    session.set_new_identity(uuid.uuid4().hex)
    session["user_id"] = user_id

    if clean_cookies:
        await req.config_dict["rd_conn"].execute(
            "EVAL", REMOVE_EXPIRED_COOKIES, 1, user_id
        )

    await req.config_dict["rd_conn"].execute(
        "SADD", f"user_cookies:{user_id}", session.identity
    )


routes = web.RouteTableDef()


@routes.get("/register")
@aiohttp_jinja2.template("auth/register.html")
async def get_register(
    req: web.Request
) -> Union[web.Response, Dict[str, Any]]:
    return {}


# TODO: propper password and login checks
@routes.post("/register")
@helpers.body_params(
    {
        "nickname": converters.String(
            strip=True, checks=[checks.LengthBetween(1, 128)]
        ),
        "email": Email(),
        "password": converters.String(checks=[checks.LengthBetween(4, 2048)]),
    },
    content_types=[ContentType.URLENCODED],
    json_response=False,
)
async def post_register(req: web.Request) -> web.Response:
    query = req["body"]

    # TODO: handle case with 2 matches
    user = await req.config_dict["pg_conn"].fetchrow(
        "SELECT id, name, email, verified FROM users WHERE name = $1 OR email = $2",
        query["nickname"],
        query["email"],
    )

    new_user_id = req.config_dict["sf_gen"].gen_id()

    if user is not None:
        if user["verified"]:
            if user["name"] == query["nickname"]:
                raise web.HTTPBadRequest(
                    reason="Nickname is already registered"
                )
            elif user["email"] == query["email"]:
                raise web.HTTPBadRequest(reason="Email is already registered")

        if (
            (new_user_id - user["id"]) >> 22
        ) > EmailConfirmationCode.life_time():
            # user not verified for duration > code life time
            await req.config_dict["pg_conn"].fetch(
                "DELETE FROM users WHERE id = $1", user["id"]
            )
        else:
            raise web.HTTPBadRequest(
                reason="User with this name or email is in registration process"
            )

    code = await EmailConfirmationCode.from_data(
        new_user_id, req.config_dict["rd_conn"]
    )

    await req.config_dict["pg_conn"].fetch(
        "INSERT INTO USERS (id, name, bot, email, password) VALUES($1, $2, $3, $4, $5)",
        new_user_id,
        query["nickname"],
        False,
        query["email"],
        bcrypt.hashpw(query["password"].encode(), bcrypt.gensalt()),
    )

    await send_email_confirmation_code(query["email"], str(code), req)

    helpers.redirect(req, "email_sent")


@routes.get("/register/email-sent", name="email_sent")
@aiohttp_jinja2.template("auth/email_sent.html")
async def get_email_send(
    req: web.Request
) -> Union[web.Response, Dict[str, Any]]:
    return {}


@routes.get("/register/confirm", name="confirm_email")
@helpers.query_params({"code": converters.String()}, json_response=False)
@aiohttp_jinja2.template("auth/email_confirm.html")
async def get_email_confirm(
    req: web.Request
) -> Union[web.Response, Dict[str, Any]]:
    try:
        code = await EmailConfirmationCode.from_string(
            req["query"]["code"], req.config_dict["rd_conn"]
        )
    except web.HTTPUnauthorized:
        return {"confirmed": False}

    await code.delete()

    # TODO: check code / handle wrong user_id errors?
    await req.config_dict["pg_conn"].fetch(
        "UPDATE users SET verified = true WHERE id = $1", code.user_id
    )

    await new_session(req, code.user_id)

    return {"confirmed": True}


@routes.get("/login", name="login")
@helpers.query_params({"redirect": converters.String(default=None)})
@aiohttp_jinja2.template("auth/login.html")
async def get_login(req: web.Request) -> Union[web.Response, Dict[str, Any]]:
    redirect = req["query"]["redirect"]

    return {
        "redirect": f"{req.scheme}://{req.host}"
        if redirect is None
        else redirect
    }


@routes.post("/login")
@helpers.body_params(
    {"login": Email(), "password": converters.String()},
    unique=True,
    content_types=[ContentType.URLENCODED],
)
async def post_login(req: web.Request) -> web.Response:
    query = req["body"]

    record = await req.config_dict["pg_conn"].fetchrow(
        "SELECT id, password FROM users WHERE email = $1", query["login"]
    )

    if record is None:
        raise web.HTTPUnauthorized()

    if not await check_user_password(query["password"], record["password"]):
        raise web.HTTPUnauthorized()

    await new_session(req, record["id"], clean_cookies=True)

    return web.Response()


@routes.get("/logout", name="logout")
async def logout(req: web.Request) -> web.Response:
    session = await aiohttp_session.get_session(req)
    if session.get("user_id") is None:
        raise web.HTTPForbidden()

    await req.config_dict["rd_conn"].execute(
        "SREM", f"user_cookies:{session['user_id']}", session.identity
    )

    session.invalidate()

    return web.HTTPFound(req.app.router["login"].url_for())


@routes.get("/reset-password")
@helpers.query_params({"code": converters.String()}, unique=True)
@aiohttp_jinja2.template("auth/reset_password.html")
async def reset_password(
    req: web.Request
) -> Union[web.Response, Dict[str, Any]]:
    query = req["query"]

    user_id = await req.config_dict["rd_conn"].execute(
        "GET", f"password_reset_code:{query['code']}"
    )

    if user_id is None:
        return web.HTTPUnauthorized(reason="Bad or expired code")

    return {"code": query["code"]}


@routes.post("/reset-password", name="reset_password")
@helpers.body_params(
    {
        "email": Email(default=None),
        "code": converters.String(default=None),
        "password": converters.String(
            checks=[checks.LengthBetween(4, 2048)], default=None
        ),
    },
    unique=True,
    content_types=[ContentType.URLENCODED],
)
async def post_reset_password(req: web.Request) -> web.Response:
    query = req["body"]

    if query["email"]:
        user = await req.config_dict["pg_conn"].fetchrow(
            "SELECT id, email, name FROM users WHERE email = $1",
            query["email"],
        )

        if user is None:
            raise web.HTTPUnauthorized(reason="Bad email")

        code = await PasswordResetCode.from_data(
            user["id"], req.config_dict["rd_conn"]
        )

        await send_password_reset_code(
            user["email"], str(code), user["name"], req
        )

        return web.Response()
    elif query["code"] and query["password"]:
        # TODO: password checks

        code = await PasswordResetCode.from_string(
            query["code"], req.config_dict["rd_conn"]
        )

        await code.delete()

        # update password
        password_hash = bcrypt.hashpw(
            query["password"].encode(), bcrypt.gensalt()
        )

        await req.config_dict["pg_conn"].fetch(
            "UPDATE users SET password = $1 WHERE id = $2",
            password_hash,
            code.user_id,
        )

        # clear all user cookies
        user_cookies = await req.config_dict["rd_conn"].execute(
            "SMEMBERS", f"user_cookies:{code.user_id}"
        )

        await req.config_dict["rd_conn"].execute(
            "DEL",
            f"user_cookies:{code.user_id}",
            *user_cookies,
            f"user_cookies:{code.user_id}",
        )

        await new_session(req, code.user_id)

        return web.Response()
    else:
        raise web.HTTPBadRequest()
