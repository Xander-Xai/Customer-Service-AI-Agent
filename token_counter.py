"""
Token 计数与中文分词工具模块
- 从 session_manager.py 拆分（v4.3）
- tiktoken token 计数
- jieba 中文分词
"""

import re

from logger import get_logger

logger = get_logger("token_counter")

# 中文分词（懒加载）
_jieba = None
_jieba_loaded = False


def _get_jieba():
    """懒加载 jieba 分词器（静默模式）"""
    global _jieba, _jieba_loaded
    if not _jieba_loaded:
        _jieba_loaded = True
        try:
            import jieba

            jieba.setLogLevel(jieba.logging.WARNING)
            _jieba = jieba
            logger.info("jieba 中文分词已加载")
        except ImportError:
            logger.warning("jieba 未安装，回退到正则分词")
            _jieba = None
    return _jieba


# tiktoken 编码器（懒加载）
_tokenizer = None
_tokenizer_loaded = False


def _get_tokenizer():
    """懒加载 tiktoken 编码器"""
    global _tokenizer, _tokenizer_loaded
    if not _tokenizer_loaded:
        _tokenizer_loaded = True
        try:
            import tiktoken

            _tokenizer = tiktoken.get_encoding("cl100k_base")
            logger.info("tiktoken 编码器已加载")
        except ImportError:
            logger.warning("tiktoken 未安装，回退到字符估算")
            _tokenizer = None
    return _tokenizer


def _tokenize_chinese(text: str) -> set:
    """中文分词 token 化（v3.4: 过滤单字停用词，与正则回退保持一致）"""
    jb = _get_jieba()
    if jb:
        return set(w for w in jb.cut(text) if len(w.strip()) >= 2)
    # 回退：正则提取中文词（2字+）和英文词
    return set(re.findall(r"[\w一-鿿]{2,}", text.lower()))


def _count_tokens(text: str) -> int:
    """计算文本 token 数（tiktoken 优先，回退字符估算）"""
    if not text:
        return 0
    tok = _get_tokenizer()
    if tok:
        return len(tok.encode(text))
    # 回退：中文约 1.5 字/token，英文约 4 字符/token
    cn_chars = len(re.findall(r"[一-鿿]", text))
    other_chars = len(text) - cn_chars
    return int(cn_chars / 1.5 + other_chars / 4)
