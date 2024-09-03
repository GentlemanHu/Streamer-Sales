#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    :   utils.py
@Time    :   2024/09/02
@Project :   https://github.com/PeterH0323/Streamer-Sales
@Author  :   HinGwenWong
@Version :   1.0
@Desc    :   工具集合文件
"""


import asyncio
import json
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import cv2
from lmdeploy.serve.openai.api_client import APIClient
from loguru import logger
from pydantic import BaseModel
from tqdm import tqdm

from ..tts.tools import SYMBOL_SPLITS, make_text_chunk
from ..web_configs import API_CONFIG, WEB_CONFIGS
from .database.product_db import get_db_product_info, save_product_info
from .database.streamer_info_db import get_streamers_info, save_streamer_info
from .database.streamer_room_db import get_streaming_room_info, update_streaming_room_info
from .modules.agent.agent_worker import get_agent_result
from .modules.rag.rag_worker import RAG_RETRIEVER, build_rag_prompt
from .queue_thread import DIGITAL_HUMAN_QUENE, TTS_TEXT_QUENE
from .server_info import SERVER_PLUGINS_INFO


class ChatGenConfig(BaseModel):
    # LLM 推理配置
    top_p: float = 0.8
    temperature: float = 0.7
    repetition_penalty: float = 1.005


class ProductInfo(BaseModel):
    name: str
    heighlights: str
    introduce: str  # 生成商品文案 prompt

    image_path: str
    departure_place: str
    delivery_company_name: str


class PluginsInfo(BaseModel):
    rag: bool = True
    agent: bool = True
    tts: bool = True
    digital_human: bool = True


class ChatItem(BaseModel):
    user_id: str  # User 识别号，用于区分不用的用户调用
    request_id: str  # 请求 ID，用于生成 TTS & 数字人
    prompt: List[Dict[str, str]]  # 本次的 prompt
    product_info: ProductInfo  # 商品信息
    plugins: PluginsInfo = PluginsInfo()  # 插件信息
    chat_config: ChatGenConfig = ChatGenConfig()


# 加载 LLM 模型
LLM_MODEL_HANDLER = APIClient(API_CONFIG.LLM_URL)


async def streamer_sales_process(chat_item: ChatItem):

    # ====================== Agent ======================
    # 调取 Agent
    agent_response = ""
    if chat_item.plugins.agent and SERVER_PLUGINS_INFO.agent_enabled:
        GENERATE_AGENT_TEMPLATE = (
            "这是网上获取到的信息：“{}”\n 客户的问题：“{}” \n 请认真阅读信息并运用你的性格进行解答。"  # Agent prompt 模板
        )
        input_prompt = chat_item.prompt[-1]["content"]
        agent_response = get_agent_result(
            LLM_MODEL_HANDLER, input_prompt, chat_item.product_info.departure_place, chat_item.product_info.delivery_company_name
        )
        if agent_response != "":
            agent_response = GENERATE_AGENT_TEMPLATE.format(agent_response, input_prompt)
            print(f"Agent response: {agent_response}")
            chat_item.prompt[-1]["content"] = agent_response

    # ====================== RAG ======================
    # 调取 rag
    if chat_item.plugins.rag and agent_response == "":
        # 如果 Agent 没有执行，则使用 RAG 查询数据库
        rag_prompt = chat_item.prompt[-1]["content"]
        prompt_pro = build_rag_prompt(RAG_RETRIEVER, chat_item.product_info.name, rag_prompt)

        if prompt_pro != "":
            chat_item.prompt[-1]["content"] = prompt_pro

    # llm 推理流返回
    logger.info(chat_item.prompt)

    current_predict = ""
    idx = 0
    last_text_index = 0
    sentence_id = 0
    model_name = LLM_MODEL_HANDLER.available_models[0]
    for item in LLM_MODEL_HANDLER.chat_completions_v1(model=model_name, messages=chat_item.prompt, stream=True):
        logger.debug(f"LLM predict: {item}")
        if "content" not in item["choices"][0]["delta"]:
            continue
        current_res = item["choices"][0]["delta"]["content"]

        if "~" in current_res:
            current_res = current_res.replace("~", "。").replace("。。", "。")

        current_predict += current_res
        idx += 1

        if chat_item.plugins.tts and SERVER_PLUGINS_INFO.tts_server_enabled:
            # 切句子
            sentence = ""
            for symbol in SYMBOL_SPLITS:
                if symbol in current_res:
                    last_text_index, sentence = make_text_chunk(current_predict, last_text_index)
                    if len(sentence) <= 3:
                        # 文字太短的情况，不做生成
                        sentence = ""
                    break

            if sentence != "":
                sentence_id += 1
                logger.info(f"get sentence: {sentence}")
                tts_request_dict = {
                    "user_id": chat_item.user_id,
                    "request_id": chat_item.request_id,
                    "sentence": sentence,
                    "chunk_id": sentence_id,
                    # "wav_save_name": chat_item.request_id + f"{str(sentence_id).zfill(8)}.wav",
                }

                TTS_TEXT_QUENE.put(tts_request_dict)
                await asyncio.sleep(0.01)

        yield json.dumps(
            {
                "event": "message",
                "retry": 100,
                "id": idx,
                "data": current_predict,
                "step": "llm",
                "end_flag": False,
            },
            ensure_ascii=False,
        )
        await asyncio.sleep(0.01)  # 加个延时避免无法发出 event stream

    if chat_item.plugins.digital_human and SERVER_PLUGINS_INFO.digital_human_server_enabled:

        wav_list = [
            Path(WEB_CONFIGS.TTS_WAV_GEN_PATH, chat_item.request_id + f"-{str(i).zfill(8)}.wav")
            for i in range(1, sentence_id + 1)
        ]
        while True:
            # 等待 TTS 生成完成
            not_exist_count = 0
            for tts_wav in wav_list:
                if not tts_wav.exists():
                    not_exist_count += 1

            logger.info(f"still need to wait for {not_exist_count}/{sentence_id} wav generating...")
            if not_exist_count == 0:
                break

            yield json.dumps(
                {
                    "event": "message",
                    "retry": 100,
                    "id": idx,
                    "data": current_predict,
                    "step": "tts",
                    "end_flag": False,
                },
                ensure_ascii=False,
            )
            await asyncio.sleep(1)  # 加个延时避免无法发出 event stream

        # 合并 tts
        tts_save_path = Path(WEB_CONFIGS.TTS_WAV_GEN_PATH, chat_item.request_id + ".wav")
        all_tts_data = []

        for wav_file in tqdm(wav_list):
            logger.info(f"Reading wav file {wav_file}...")
            with wave.open(str(wav_file), "rb") as wf:
                all_tts_data.append([wf.getparams(), wf.readframes(wf.getnframes())])

        logger.info(f"Merging wav file to {tts_save_path}...")
        tts_params = max([tts_data[0] for tts_data in all_tts_data])
        with wave.open(str(tts_save_path), "wb") as wf:
            wf.setparams(tts_params)  # 使用第一个音频参数

            for wf_data in all_tts_data:
                wf.writeframes(wf_data[1])
        logger.info(f"Merged wav file to {tts_save_path} !")

        # 生成数字人视频
        tts_request_dict = {
            "user_id": chat_item.user_id,
            "request_id": chat_item.request_id,
            "chunk_id": 0,
            "tts_path": str(tts_save_path),
        }

        logger.info(f"Generating digital human...")
        DIGITAL_HUMAN_QUENE.put(tts_request_dict)
        while True:
            if (
                Path(WEB_CONFIGS.DIGITAL_HUMAN_VIDEO_OUTPUT_PATH)
                .joinpath(Path(tts_save_path).stem + ".mp4")
                .with_suffix(".txt")
                .exists()
            ):
                break
            yield json.dumps(
                {
                    "event": "message",
                    "retry": 100,
                    "id": idx,
                    "data": current_predict,
                    "step": "dg",
                    "end_flag": False,
                },
                ensure_ascii=False,
            )
            await asyncio.sleep(1)  # 加个延时避免无法发出 event stream

        # 删除过程文件
        for wav_file in wav_list:
            wav_file.unlink()

    yield json.dumps(
        {
            "event": "message",
            "retry": 100,
            "id": idx,
            "data": current_predict,
            "step": "all",
            "end_flag": True,
        },
        ensure_ascii=False,
    )


def make_poster_by_video_first_frame(video_path: str, image_output_name: str):
    """根据视频第一帧生成缩略图

    Args:
        video_path (str): 视频文件路径

    Returns:
        str: 第一帧保存的图片路径
    """

    # 打开视频文件
    cap = cv2.VideoCapture(video_path)

    # 读取第一帧
    ret, frame = cap.read()

    # 检查是否成功读取
    poster_save_path = str(Path(video_path).parent.joinpath(image_output_name))
    if ret:
        # 保存图像到文件
        cv2.imwrite(poster_save_path, frame)
        logger.info(f"第一帧已保存为 {poster_save_path}")
    else:
        logger.error("无法读取视频帧")

    # 释放视频捕获对象
    cap.release()

    return poster_save_path


async def delete_item_by_id(item_type: str, delete_id: int, user_id: int = 0):
    """根据类型删除某个ID的信息"""

    logger.info(delete_id)

    assert item_type in ["product", "streamer", "room"]

    get_func_map = {
        "product": get_db_product_info,
        "streamer": get_streamers_info,
        "room": get_streaming_room_info,
    }

    save_func_map = {"product": save_product_info, "streamer": save_streamer_info, "room": update_streaming_room_info}

    id_name_map = {
        "product": "product_id",
        "streamer": "id",
        "room": "room_id",
    }

    item_list = await get_func_map[item_type](user_id)
    logger.info(item_list)

    process_success_flag = False

    save_info = None
    if item_type == "product":
        for product_name, product_info in item_list.items():
            if product_info[id_name_map[item_type]] != delete_id:
                continue

            item_list[product_name]["delete"] = True
            item_list[product_name].update({"product_name": product_name})
            save_info = item_list[product_name]
            process_success_flag = True
            break
    else:
        for idx, item in enumerate(item_list):
            if item[id_name_map[item_type]] != delete_id:
                continue

            item_list[idx]["delete"] = True
            save_info = item_list[idx]
            process_success_flag = True
            break

    # 保存
    save_func_map[item_type](save_info)

    return process_success_flag


@dataclass
class ResultCode:
    SUCCESS: int = 0000  # 成功
    FAIL: int = 1000  # 失败


def make_return_data(success_flag: bool, code: ResultCode, message: str, data: dict):
    return {
        "success": success_flag,
        "code": code,
        "message": message,
        "data": data,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
