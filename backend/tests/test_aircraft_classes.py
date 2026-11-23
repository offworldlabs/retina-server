from config.aircraft_classes import class_for_type


def test_class_for_type_resolves_each_class_and_falls_back_to_unknown():
    assert class_for_type("C130") == "military_transport"
    assert class_for_type("B77W") == "widebody"
    assert class_for_type("CRJ9") == "regional_jet"
    assert class_for_type("B738") == "narrowbody"
    assert class_for_type("AT76") == "turboprop"
    assert class_for_type("GLF6") == "bizjet"
    assert class_for_type("C172") == "light_GA"
    assert class_for_type("R44") == "helicopter"
    assert class_for_type("S22T") == "light_GA"
    assert class_for_type("B350") == "turboprop"
    assert class_for_type(" b738 ") == "narrowbody"
    assert class_for_type("ZZZZ") == "unknown"
    assert class_for_type(None) == "unknown"
