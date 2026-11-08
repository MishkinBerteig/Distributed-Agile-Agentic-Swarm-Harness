from app import routing
from app.models import HarnessSpec, RoleModels, SwarmConfig


def cfg(**kw):
    return SwarmConfig(**kw)


# --- ladder ------------------------------------------------------------------

def test_ladder_default():
    assert routing.ladder(cfg()) == ["instant", "fast", "great", "amazing"]


def test_ladder_filters_and_keeps_ladder_order():
    c = cfg(allowed_models=["amazing", "fast"])
    assert routing.ladder(c) == ["fast", "amazing"]


def test_ladder_falls_back_to_allowed_when_no_overlap():
    c = cfg(allowed_models=["x", "y"], model_ladder=["a", "b"])
    assert routing.ladder(c) == ["x", "y"]
    assert routing.ladder(cfg(allowed_models=["x"], model_ladder=[])) == ["x"]


# --- choose_model: non-delivery ---------------------------------------------

def test_judges_classifier_coordinator_use_configured_models():
    c = cfg()
    assert routing.choose_model("mission_judgement", c) == "great"
    assert routing.choose_model("quality_judgement", c) == "great"
    assert routing.choose_model("classifier", c) == "instant"
    assert routing.choose_model("swarm_coordinator", c) == "amazing"


def test_judges_ignore_complexity_and_attempts():
    c = cfg()
    assert routing.choose_model("quality_judgement", c, "complex", 5) == "great"


def test_unknown_role_raises():
    try:
        routing.choose_model("janitor", cfg())
    except ValueError as e:
        assert "janitor" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_disallowed_configured_model_falls_back_to_nearest():
    c = cfg(allowed_models=["instant", "amazing"],
            role_models=RoleModels(mission_judgement="great", quality_judgement="fast",
                                   mission_delivery="instant", classifier="instant"),
            coordinator=HarnessSpec(model="amazing"))
    # great (pos 2): instant dist 2, amazing dist 1 -> amazing
    assert routing.choose_model("mission_judgement", c) == "amazing"
    # fast (pos 1): instant dist 1, amazing dist 2 -> instant
    assert routing.choose_model("quality_judgement", c) == "instant"


def test_disallowed_tie_prefers_more_capable():
    c = cfg(allowed_models=["instant", "great"],
            role_models=RoleModels(mission_judgement="fast"))
    assert routing.choose_model("mission_judgement", c) == "great"


def test_unknown_model_not_on_ladder_uses_top_allowed():
    c = cfg(role_models=RoleModels(mission_judgement="mystery"))
    assert routing.choose_model("mission_judgement", c) == "amazing"


# --- choose_model: delivery --------------------------------------------------

def test_delivery_base_and_complexity():
    c = cfg()  # delivery base = fast
    assert routing.choose_model("mission_delivery", c) == "fast"
    assert routing.choose_model("mission_delivery", c, "simple") == "fast"
    assert routing.choose_model("mission_delivery", c, "moderate") == "fast"
    assert routing.choose_model("mission_delivery", c, None) == "fast"
    assert routing.choose_model("mission_delivery", c, "weird") == "fast"
    assert routing.choose_model("mission_delivery", c, "complex") == "great"


def test_delivery_escalates_per_attempt_and_caps():
    c = cfg()
    assert [routing.choose_model("mission_delivery", c, "moderate", a) for a in range(5)] == \
        ["fast", "great", "amazing", "amazing", "amazing"]
    assert routing.choose_model("mission_delivery", c, "complex", 1) == "amazing"
    assert routing.choose_model("mission_delivery", c, "complex", 10) == "amazing"
    assert routing.choose_model("mission_delivery", c, "simple", -3) == "fast"


def test_delivery_respects_filtered_ladder():
    c = cfg(allowed_models=["fast", "amazing"])
    assert routing.choose_model("mission_delivery", c, "moderate", 0) == "fast"
    assert routing.choose_model("mission_delivery", c, "moderate", 1) == "amazing"


def test_delivery_base_not_allowed_uses_nearest_rung():
    c = cfg(allowed_models=["instant", "great"],
            role_models=RoleModels(mission_delivery="fast"))
    assert routing.choose_model("mission_delivery", c) == "great"      # tie -> more capable
    assert routing.choose_model("mission_delivery", c, attempts=1) == "great"  # already top


def test_delivery_single_model_ladder():
    c = cfg(allowed_models=["only"], model_ladder=["only"],
            role_models=RoleModels(mission_delivery="only"))
    assert routing.choose_model("mission_delivery", c, "complex", 4) == "only"


def test_delivery_learned_bump():
    c = cfg()
    stats = {("fast", "moderate"): (1, 5)}                    # 20% success, 5 samples
    assert routing.choose_model("mission_delivery", c, "moderate", 0, stats) == "great"
    assert routing.choose_model("mission_delivery", c, None, 0, stats) == "great"
    assert routing.choose_model("mission_delivery", c, "moderate", 1, stats) == "amazing"


def test_delivery_learned_bump_thresholds():
    c = cfg()
    assert routing.choose_model("mission_delivery", c, "moderate", 0,
                                {("fast", "moderate"): (0, 4)}) == "fast"   # too few samples
    assert routing.choose_model("mission_delivery", c, "moderate", 0,
                                {("fast", "moderate"): (5, 10)}) == "fast"  # exactly 0.5 ok
    assert routing.choose_model("mission_delivery", c, "moderate", 0,
                                {("fast", "moderate"): (4, 10)}) == "great"
    assert routing.choose_model("mission_delivery", c, "simple", 0,
                                {("fast", "moderate"): (0, 9)}) == "fast"   # other complexity


def test_delivery_learned_bump_uses_model_at_start_rung_for_complex():
    c = cfg()
    # complex starts at "great"; stats about "fast" are irrelevant
    assert routing.choose_model("mission_delivery", c, "complex", 0,
                                {("fast", "complex"): (0, 9)}) == "great"
    assert routing.choose_model("mission_delivery", c, "complex", 0,
                                {("great", "complex"): (0, 9)}) == "amazing"


# --- routing_stats -----------------------------------------------------------

def test_routing_stats():
    s = routing.routing_stats([("fast", "simple", True), ("fast", "simple", False),
                               ("fast", None, True), ("fast", "moderate", False),
                               ("great", "complex", True)])
    assert s == {("fast", "simple"): (1, 2), ("fast", "moderate"): (1, 2),
                 ("great", "complex"): (1, 1)}


def test_routing_stats_empty_and_generator():
    assert routing.routing_stats([]) == {}
    assert routing.routing_stats(x for x in [("m", "simple", True)]) == {("m", "simple"): (1, 1)}


# --- validate_config ---------------------------------------------------------

def test_validate_default_ok():
    assert routing.validate_config(cfg()) == []


def test_validate_role_model_not_allowed():
    c = cfg(allowed_models=["fast"], model_ladder=["fast"])
    errs = routing.validate_config(c)
    assert any("role_models.mission_judgement 'great'" in e for e in errs)
    assert any("coordinator.model 'amazing'" in e for e in errs)
    assert not any("mission_delivery" in e for e in errs)


def test_validate_coordinator_harness_provider():
    c = cfg(coordinator=HarnessSpec(harness="claude", provider="acme"))
    errs = routing.validate_config(c)
    assert any("coordinator.harness 'claude'" in e for e in errs)
    assert any("coordinator.provider 'acme'" in e for e in errs)


def test_validate_empty_lists():
    c = cfg(allowed_models=[], allowed_harnesses=[], allowed_providers=[], model_ladder=[])
    errs = routing.validate_config(c)
    assert any("allowed_models is empty" in e for e in errs)
    assert any("allowed_harnesses is empty" in e for e in errs)
    assert any("allowed_providers is empty" in e for e in errs)
    assert any("model_ladder is empty" in e for e in errs)


def test_validate_ladder_without_overlap():
    errs = routing.validate_config(cfg(model_ladder=["zzz"]))
    assert any("shares no model" in e for e in errs)


def test_validate_prices_and_thresholds():
    c = cfg(model_prices={"fast": -1.0})
    c.dod_thresholds.rubric = 1.5
    errs = routing.validate_config(c)
    assert any("model_prices['fast']" in e for e in errs)
    assert any("dod_thresholds.rubric" in e for e in errs)
