#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    :   users.py
@Time    :   2024/08/30
@Project :   https://github.com/PeterH0323/Streamer-Sales
@Author  :   HinGwenWong
@Version :   1.0
@Desc    :   用户登录和 Token 认证接口
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from loguru import logger
from passlib.context import CryptContext

from ...web_configs import WEB_CONFIGS
from ..database.user_db import fake_users_db
from ..models.user_model import TokenItem, UserInfo
from ..utils import ResultCode, make_return_data

router = APIRouter(
    prefix="/user",
    tags=["user"],
    responses={404: {"description": "Not found"}},
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/user/login")


PWD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password, hashed_password):
    logger.info(f"expect password = {PWD_CONTEXT.hash('123456')}")
    return PWD_CONTEXT.verify(plain_password, hashed_password)


def get_password_hash(password):
    return PWD_CONTEXT.hash(password)


def get_user(db: dict, username: str = "", user_id: int = -1):

    if user_id > 0:
        # 使用 ID 查询
        for _, user_info in db.items():
            if user_id == user_info["user_id"]:
                return user_info

    if username != "" and username in db:
        # 使用用户名查询
        user_dict = db[username]
        return UserInfo(**user_dict)
    return None


def authenticate_user(db_name, username: str, password: str):
    # 获取用户信息
    user_info = get_user(db_name, username=username)
    if not user_info:
        # 没有找到用户名
        logger.info(f"Cannot find username = {username}")
        return False

    # 校验密码
    if not verify_password(password, user_info.hashed_password):
        logger.info(f"verify_password fail")
        # 密码校验失败
        return False

    return user_info


def get_current_user_info(token: str = Depends(oauth2_scheme)):
    logger.info(token)
    try:
        token_data = jwt.decode(token, WEB_CONFIGS.TOKEN_JWT_SECURITY_KEY, algorithms=WEB_CONFIGS.TOKEN_JWT_ALGORITHM)
        logger.info(token_data)
        user_id = token_data.get("user_id", None)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    if not user_id:
        logger.error(f"can not get user_id: {user_id}")
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    # TODO 超时强制重新登录

    logger.info(f"Got user_id: {user_id}")
    return user_id


@router.post("/login", summary="登录接口")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):

    # 校验用户名和密码
    user_info = authenticate_user(fake_users_db, form_data.username, form_data.password)

    if not user_info:
        raise HTTPException(status_code=401, detail="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})

    # 过期时间
    token_expires = datetime.now(timezone.utc) + timedelta(days=7)

    # token 生成包含内容，记录 IP 的原因是防止被其他人拿到用户的 token 进行假冒访问
    token_data = {
        "user_id": user_info.user_id,
        "username": user_info.username,
        "exp": int(token_expires.timestamp()),
        "ip": user_info.ip_adress,
        "login_time": int(datetime.now(timezone.utc).timestamp()),
    }
    logger.info(f"token_data = {token_data}")

    # 生成 token
    token = jwt.encode(token_data, WEB_CONFIGS.TOKEN_JWT_SECURITY_KEY, algorithm=WEB_CONFIGS.TOKEN_JWT_ALGORITHM)

    # 返回
    res_json = TokenItem(access_token=token, token_type="bearer")
    logger.info(f"Got token info = {res_json}")
    # return make_return_data(True, ResultCode.SUCCESS, "成功", content)
    return res_json


@router.post("/me", summary="获取用户信息")
async def get_streaming_room_api(user_id: int = Depends(get_current_user_info)):
    """获取用户信息"""
    user_info = get_user(fake_users_db, user_id=user_id)
    return make_return_data(True, ResultCode.SUCCESS, "成功", user_info)