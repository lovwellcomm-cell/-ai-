"""ui/payload.py 的测试。

这个模块刻意做成不依赖 Streamlit 的纯函数，就是为了能在这里覆盖 ——
app.py 里的胶水层没法单测，能测的逻辑都挤到 payload 里了。
"""
import pytest

from ui import payload


def _hit(n, section="第三节 管理层讨论与分析", sub="主营业务分析", p1=24, p2=25, text="片段正文"):
    return {"chunk_id": n, "section": section, "subsection": sub,
            "page_start": p1, "page_end": p2, "text": text}


class TestToSources:
    def test_numbers_from_one_in_retrieval_order(self):
        hits = [_hit(7), _hit(3), _hit(11)]
        sources = payload.to_sources(hits, "")
        assert [s["n"] for s in sources] == [1, 2, 3]
        # 页码取自原始片段，不受重新编号影响
        assert sources[0]["p1"] == 24

    def test_marks_only_cited_chunks(self):
        hits = [_hit(1), _hit(2), _hit(3)]
        sources = payload.to_sources(hits, "营业收入 8.09 亿元[2]，毛利率 11.7%[3][1]。")
        assert [s["cited"] for s in sources] == [True, True, True]

        sources = payload.to_sources(hits, "只引用了第三段[3]。")
        assert [s["cited"] for s in sources] == [False, False, True]

    def test_out_of_range_citation_is_ignored(self):
        """回答里出现 [9] 但只检索到 3 段时，不能凭空多出一个来源。"""
        sources = payload.to_sources([_hit(1), _hit(2), _hit(3)], "见[9]")
        assert len(sources) == 3
        assert not any(s["cited"] for s in sources)

    def test_missing_fields_degrade_gracefully(self):
        sources = payload.to_sources([{"page_start": 5, "page_end": 5, "text": "只有页码"}], "")
        assert sources[0]["section"] == "正文"
        assert sources[0]["sub"] == ""
        assert sources[0]["p1"] == sources[0]["p2"] == 5


class TestAiMessage:
    def test_done_message_carries_answer_and_sources(self):
        turn = {"q": "问题", "a": "回答[1]", "hits": [_hit(1)], "queries": ["检索词"]}
        msg = payload.ai_message(turn)
        assert msg["role"] == "ai"
        assert msg["status"] == "done"
        assert msg["text"] == "回答[1]"
        assert msg["hits"] == 1
        assert msg["queries"] == ["检索词"]
        assert msg["sources"][0]["cited"] is True
        assert "statusText" not in msg

    @pytest.mark.parametrize("status", ["retrieving", "generating"])
    def test_pending_status_gets_a_caption(self, status):
        msg = payload.ai_message({"q": "问题", "status": status})
        assert msg["status"] == status
        assert msg["statusText"] == payload.STATUS_TEXT[status]

    def test_error_message_is_rendered_but_not_busy(self):
        msg = payload.ai_message({"q": "问题", "status": "error", "error": "调用失败"})
        assert msg["error"] == "调用失败"
        assert msg["text"] == ""

    def test_structured_card_fields_pass_through_only_when_present(self):
        """后端目前不产出 kpis/charts/table，组件收到才渲染。"""
        assert "kpis" not in payload.ai_message({"q": "q", "a": "a"})
        msg = payload.ai_message({"q": "q", "a": "a", "kpis": [{"k": "营业收入", "v": "1.0"}]})
        assert msg["kpis"][0]["k"] == "营业收入"


class TestBuildMessages:
    def test_history_then_pending_turn(self):
        history = [{"q": "旧问题", "a": "旧回答", "hits": [_hit(1)], "queries": []}]
        turn = {"q": "新问题", "status": "retrieving"}
        messages = payload.build_messages(history, turn)
        assert [m["role"] for m in messages] == ["user", "ai", "user", "ai"]
        assert messages[0]["text"] == "旧问题"
        assert messages[3]["status"] == "retrieving"

    def test_page_attaches_to_the_turn_when_one_is_running(self):
        page = {"n": 2, "png": "data:image/png;base64,AAA"}
        messages = payload.build_messages([], {"q": "问", "status": "generating"}, page=page)
        assert messages[-1]["page"] == page

    def test_page_attaches_to_last_history_message_when_idle(self):
        history = [{"q": "问", "a": "答", "hits": [_hit(1)], "queries": []}]
        page = {"n": 1, "png": "data:image/png;base64,AAA"}
        messages = payload.build_messages(history, None, page=page)
        assert messages[-1]["page"] == page

    def test_no_page_means_no_key(self):
        history = [{"q": "问", "a": "答", "hits": [_hit(1)], "queries": []}]
        assert "page" not in payload.build_messages(history, None)[-1]


class TestJsonSafe:
    def test_nested_payload_survives_round_trip(self):
        data = {"a": [1, {"b": "中文"}], "c": None, "d": True}
        assert payload.json_safe(data) == data

    def test_non_serialisable_value_raises_instead_of_degrading_silently(self):
        """真实踩过的坑：payload 里混进 set / Path / numpy 标量时，Streamlit 的兜底是把
        整个 data 静默变成一段 Python repr 字符串，前端只会看到 undefined 且不报错。
        这里必须响亮地失败。"""
        for bad in ({"x": {1, 2}}, {"x": object()}):
            with pytest.raises(TypeError):
                payload.json_safe(bad)

    def test_nan_raises_because_it_is_not_valid_json(self):
        with pytest.raises(ValueError):
            payload.json_safe({"x": float("nan")})


class TestBuildData:
    def test_doc_summary_drives_hero_copy(self):
        data = payload.build_data(
            doc={"company": "精达股份", "year": "2025", "name": "精达股份2025年报.pdf",
                 "pages": 246, "chunks": 471},
            messages=[],
        )
        assert data["doc"]["pages"] == 246
        assert "精达股份 2025" in data["heroEyebrow"]
        assert "246 页" in data["heroFoot"]
        assert "精达股份2025年报.pdf" in data["placeholder"]
        assert data["theme"] == "light"

    def test_unknown_theme_falls_back_to_light(self):
        data = payload.build_data(doc={}, messages=[], theme="solarized")
        assert data["theme"] == "light"

    def test_output_is_json_serialisable(self):
        import json
        data = payload.build_data(doc={}, messages=[payload.ai_message({"q": "q", "a": "a"})])
        json.dumps(data)  # 不应抛异常
