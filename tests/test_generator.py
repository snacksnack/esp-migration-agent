from seeding.generator import generate


def test_deterministic_for_a_given_seed():
    subs_a, truth_a = generate(count=120, seed=7)
    subs_b, truth_b = generate(count=120, seed=7)
    assert subs_a == subs_b
    assert truth_a == truth_b


def test_count_and_unique_emails():
    subs, truth = generate(count=450, seed=542)
    assert len(subs) == truth.count == 450
    assert len({s.email for s in subs}) == 450


def test_aggregates_count_every_record():
    _, truth = generate(count=450, seed=542)
    for dist in (truth.domain_class, truth.consent, truth.tenure_cohorts, truth.states):
        assert sum(dist.values()) == truth.count


def test_distributions_near_nominal_rates():
    _, truth = generate(count=450, seed=542)
    assert abs(truth.domain_class["consumer"] / truth.count - 0.65) < 0.08
    assert abs(truth.consent["subscribed"] / truth.count - 0.84) < 0.08
    assert max(truth.tenure_cohorts, key=truth.tenure_cohorts.get) == "1_3y"
    assert max(truth.states, key=truth.states.get) == "NY"


def test_every_seeded_risk_class_present():
    _, truth = generate(count=450, seed=542)
    assert set(truth.risk_counts) == {"role_address", "missing_fname", "bad_zip", "messy_date"}
    assert all(v > 0 for v in truth.risk_counts.values())


def test_suppression_ground_truth_is_the_unsubscribed_set():
    subs, truth = generate(count=450, seed=542)
    unsubscribed = sorted(s.email for s in subs if s.status == "unsubscribed")
    assert truth.suppressed_emails == unsubscribed
    assert truth.suppressed_emails  # non-empty: the invariant has something to protect
