"""
v3.1 改进项专项测试
验证：jieba 分词回退、扩充矛盾检测、多分类意图漂移、token 计数、漂移升级、客户资料查询
无需 LLM API 和网络
"""
import os
import sys
import asyncio

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def test_improvements():
    print("=" * 60)
    print("v3.1 改进项专项测试")
    print("=" * 60)
    passed = 0
    failed = 0

    # ---- Test 1: 模块导入 ----
    print("\n[1] 模块导入...")
    try:
        from session_manager import (
            EnhancedSessionManager, DriftType, NEGATION_PAIRS, INTENT_KEYWORDS,
            _tokenize_chinese, _count_tokens, _get_jieba, _get_tokenizer,
            NEGATION_PAIRS as np,
        )
        from agents.billing_agent import BillingAgent
        from agents.general_agent import GeneralAgent
        from config import SESSION_MAX_TOKENS, SESSION_SUMMARY_MAX_CHARS
        print(f"    ✓ 所有模块导入成功")
        print(f"    SESSION_MAX_TOKENS={SESSION_MAX_TOKENS}")
        print(f"    SESSION_SUMMARY_MAX_CHARS={SESSION_SUMMARY_MAX_CHARS}")
        passed += 1
    except Exception as e:
        print(f"    ✗ 导入失败: {e}")
        failed += 1
        return

    # ---- Test 2: jieba 回退分词 ----
    print("\n[2] 中文分词（jieba 回退正则）...")
    try:
        tokens = _tokenize_chinese("玫瑰精华液多少钱？")
        print(f"    分词结果: {tokens}")
        assert len(tokens) > 0, "分词结果为空"
        print(f"    ✓ 分词正常（当前模式: {'jieba' if _get_jieba() else '正则回退'}）")
        passed += 1
    except Exception as e:
        print(f"    ✗ 分词失败: {e}")
        failed += 1

    # ---- Test 3: token 计数 ----
    print("\n[3] Token 计数...")
    try:
        t1 = _count_tokens("Hello world")
        t2 = _count_tokens("这是一段中文测试文本，用于验证token计数功能")
        t3 = _count_tokens("")
        print(f"    英文: {t1} tokens")
        print(f"    中文: {t2} tokens")
        print(f"    空串: {t3} tokens")
        assert t1 > 0, "英文 token 数应 > 0"
        assert t2 > 0, "中文 token 数应 > 0"
        assert t3 == 0, "空串 token 数应为 0"
        print(f"    ✓ Token 计数正常（当前模式: {'tiktoken' if _get_tokenizer() else '字符估算'}）")
        passed += 1
    except Exception as e:
        print(f"    ✗ Token 计数失败: {e}")
        failed += 1

    # ---- Test 4: 扩充矛盾检测 ----
    print("\n[4] 扩充矛盾检测（40+ 组反义词）...")
    try:
        print(f"    反义词对总数: {len(NEGATION_PAIRS)}")
        assert len(NEGATION_PAIRS) >= 40, f"反义词对不足 40 组: {len(NEGATION_PAIRS)}"
        # 验证几组关键的
        pairs_dict = dict(NEGATION_PAIRS)
        assert pairs_dict.get("好") == "差"
        assert pairs_dict.get("可以退") == "不能退"
        assert pairs_dict.get("正品") == "假货"
        print(f"    ✓ 扩充矛盾检测就绪 ({len(NEGATION_PAIRS)} 组)")
        passed += 1
    except Exception as e:
        print(f"    ✗ 矛盾检测验证失败: {e}")
        failed += 1

    # ---- Test 5: 多分类意图识别 ----
    print("\n[5] 多分类意图识别（7 类）...")
    try:
        sm = EnhancedSessionManager(window_size=5)

        # 测试不同意图分类
        test_cases = [
            ("这款精华液有什么成分", "product_info"),
            ("产品用了过敏怎么办", "technical_support"),
            ("我要退款", "billing"),
            ("我要投诉你们的服务", "complaint"),
            ("我的订单到哪了", "order_query"),
            ("我是油性皮肤推荐什么", "cosmetic_advice"),
            ("你好想咨询一下", "general_inquiry"),
        ]
        correct = 0
        for text, expected in test_cases:
            result = sm._classify_intent(text)
            match = "✓" if result == expected else "✗"
            if result == expected:
                correct += 1
            print(f"    {match} '{text[:15]}...' → {result} (期望: {expected})")

        print(f"    意图识别准确率: {correct}/{len(test_cases)}")
        assert correct >= 5, f"意图识别准确率过低: {correct}/{len(test_cases)}"
        print(f"    ✓ 多分类意图识别正常")
        passed += 1
    except Exception as e:
        print(f"    ✗ 意图识别失败: {e}")
        failed += 1

    # ---- Test 6: 漂移检测集成 ----
    print("\n[6] 漂移检测集成测试...")
    try:
        sm = EnhancedSessionManager(window_size=5)
        sm.create_session("drift_test")

        # 场景1: 正常对话
        sm.add_message("drift_test", "玫瑰精华液多少钱", is_user=True)
        sm.add_message("drift_test", "298元", is_user=False)
        result = sm.detect_drift("drift_test", "它的成分是什么")
        print(f"    正常对话: has_drift={result['has_drift']}")

        # 场景2: 意图漂移（产品咨询 → 投诉）
        sm.create_session("intent_test")
        sm.add_message("intent_test", "这款精华液多少钱", is_user=True)
        sm.add_message("intent_test", "298元", is_user=False)
        sm.add_message("intent_test", "它的成分安全吗", is_user=True)
        sm.add_message("intent_test", "安全的，通过了检测", is_user=False)
        result = sm.detect_drift("intent_test", "我要投诉你们的服务态度太差了")
        has_intent = any(d["type"] == "intent_drift" for d in result["drifts"])
        print(f"    意图漂移(产品咨询→投诉): has_drift={result['has_drift']}, has_intent_drift={has_intent}")
        assert has_intent, "应检测到 product_info → complaint 意图漂移"

        # 场景3: 矛盾检测（新增的反义词对）
        sm.create_session("contra_test")
        sm.add_message("contra_test", "这个产品是正品吗", is_user=True)
        sm.add_message("contra_test", "是的，保证正品", is_user=False)
        sm.add_message("contra_test", "感觉效果还可以", is_user=True)
        sm.add_message("contra_test", "感谢您的认可", is_user=False)
        result = sm.detect_drift("contra_test", "我觉得这是假货")
        has_contra = any(d["type"] == "contradiction" for d in result["drifts"])
        print(f"    矛盾检测(正品/假货): has_drift={result['has_drift']}, has_contradiction={has_contra}")
        assert has_contra, "应检测到'正品' vs '假货'矛盾"

        # 场景4: 漂移升级
        sm.create_session("escalation_test")
        # 需要先添加消息（否则 topic_history < 2 会提前返回）
        sm.add_message("escalation_test", "问题一", is_user=True)
        sm.add_message("escalation_test", "回答一", is_user=False)
        # 模拟 6 次漂移
        for i in range(6):
            sm.sessions["escalation_test"]["drift_log"].append({
                "type": "topic_drift", "detail": f"test drift {i}"
            })
        result = sm.detect_drift("escalation_test", "随便问个问题")
        has_escalation = result.get("escalation") and result["escalation"].get("escalate")
        print(f"    漂移升级(6次): escalation={has_escalation}")
        assert has_escalation, "应触发漂移升级"

        print(f"    ✓ 漂移检测集成通过")
        passed += 1
    except Exception as e:
        print(f"    ✗ 漂移检测失败: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    # ---- Test 7: Token 级滑动窗口 ----
    print("\n[7] Token 级滑动窗口...")
    try:
        sm = EnhancedSessionManager(window_size=3, max_tokens=50)
        sm.create_session("token_test")
        # 添加足够多的消息触发 token 裁剪
        for i in range(10):
            sm.add_message("token_test", f"这是第{i+1}条消息，用于测试token级别的滑动窗口裁剪功能", is_user=(i % 2 == 0))

        context = await sm.get_conversation_context("token_test")
        total_chars = sum(len(m.get("content", "")) for m in context)
        total_tokens = sum(_count_tokens(m.get("content", "")) for m in context)
        print(f"    消息总数: 10, 上下文消息数: {len(context)}, tokens: {total_tokens}, chars: {total_chars}")
        assert total_tokens <= 65, f"Token 超限: {total_tokens} > 65（允许估算误差）"
        # 验证摘要已生成（如果有 old_messages 被裁剪）
        session = sm.get_session("token_test")
        print(f"    摘要: {session.get('summary', '(无)')[:50]}")
        print(f"    ✓ Token 级裁剪正常")
        passed += 1
    except Exception as e:
        print(f"    ✗ Token 裁剪失败: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    # ---- Test 8: 客户资料 ERP 查询 ----
    print("\n[8] 客户资料 ERP 查询...")
    try:
        from agents.billing_agent import BillingAgent
        from agents.general_agent import GeneralAgent
        from erp.kingdee_adapter import KingdeeMockAdapter

        erp = KingdeeMockAdapter()

        # BillingAgent 查询客户
        ba = BillingAgent()
        ba.set_erp(erp)
        result = await ba._query_erp("查询订单ORD20260530001")
        print(f"    BillingAgent 结果:\n      {result.replace(chr(10), chr(10) + '      ')}")
        assert "王女士" in result, "应包含客户名"

        # BillingAgent 通过客户ID查询
        result2 = await ba._query_erp("查询客户C002的订单")
        print(f"    BillingAgent(C002):\n      {result2.replace(chr(10), chr(10) + '      ')}")
        assert "李女士" in result2, "应包含客户名C002"

        # GeneralAgent 查询客户
        ga = GeneralAgent()
        ga.set_erp(erp)
        customer = await erp.query_customer("C001")
        assert customer is not None
        assert customer["name"] == "王女士"
        print(f"    GeneralAgent ERP query_customer(C001): {customer['name']}")

        print(f"    ✓ 客户资料 ERP 查询正常")
        passed += 1
    except Exception as e:
        print(f"    ✗ 客户资料查询失败: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    # ---- Test 9: 会话信息含漂移升级 ----
    print("\n[9] 会话信息含漂移升级状态...")
    try:
        sm = EnhancedSessionManager(window_size=5)
        sm.create_session("info_test")
        # 模拟 5 次漂移
        sm.sessions["info_test"]["drift_log"] = [
            {"type": "topic_drift"} for _ in range(5)
        ]
        info = sm.get_session_info("info_test")
        print(f"    drift_count={info['drift_count']}, escalation={info['drift_escalation']}")
        assert info["drift_escalation"] == True
        print(f"    ✓ 会话信息包含升级状态")
        passed += 1
    except Exception as e:
        print(f"    ✗ 会话信息测试失败: {e}")
        failed += 1

    # ---- 总结 ----
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败, 共 {passed + failed} 项")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(test_improvements())
