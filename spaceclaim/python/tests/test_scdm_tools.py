"""Offline tests for the creation tools and the seven measured defects.

Everything here runs without ANSYS, without a licence and without a network call.
The pattern is the same as ``test_gui_modes.py``: a fake stands in for PyAnsys
and records what the server was willing to do, because that is where the
substance is.

The seven defects these tests pin down, all reproduced on a real spaceClaim 24.2
user session during a pipe-modelling run:

1. ``scdm_run_script``'s ``script`` parameter was really a **file path**, so
   script text failed with ``script not found``.
2. ``scdm_export`` treated ``location`` as a **directory**, so the file name
   followed the document (``设计(D)1``) and ``.step`` became ``.stp``.
3. ``scdm_inspect_geometry``'s ``bodies_checked`` / ``faces_checked`` were
   **exactly twice** the truth, while ``scdm_list_bodies`` said otherwise.
4. No tool could **create** geometry.
5. ``CreatePlanarBody(plane, PointUV outer, innerLoops)`` is not implemented on
   ACIS and must be reported as a kernel limitation, not echoed raw.
6. ``removeInnerRegions=False`` drops the inner loop **silently**; a creation tool
   must therefore read the section area back.
7. ``short_edges`` reported 1440 tessellation chords with no attribution.
"""

from __future__ import annotations

import inspect

import pytest

from ansys_bridge_mcp import scdm, server


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFace:
    """A face with an id, a surface type and faces-in-a-body, like the real one."""

    def __init__(self, fid: str, surface: str = "PLANE", area_m2: float = 0.001):
        self.id = fid
        self.surface_type = surface
        self._area = area_m2
        self.body = None

    @property
    def area(self):
        class _Q:
            magnitude = 0.001

        q = _Q()
        q.magnitude = self._area
        return q


class FakeQuantity:
    """Stands in for a PyAnsys ``Quantity``: the number is at ``.magnitude``."""

    def __init__(self, magnitude: float):
        self.magnitude = magnitude


class FakeEdge:
    def __init__(self, eid: str, length_m: float, curve_type: str = "CURVETYPE_LINE"):
        self.id = eid
        self.curve_type = curve_type
        self._length = length_m

    @property
    def length(self):
        return FakeQuantity(self._length)


class FakeShortEdgeProblem:
    """One ``ShortEdgeProblemAreas`` group: it carries a list of edges."""

    def __init__(self, pid: str, edges: list[FakeEdge]):
        self.id = pid
        self.edges = edges


class FakeBody:
    def __init__(self, name: str, faces: int = 1, volume_m3: float = 1e-4,
                 surface: str = "PLANE"):
        self.name = name
        self._faces = [FakeFace(f"{name}:{i}", surface=surface) for i in range(faces)]
        for face in self._faces:
            face.body = self
        self._volume = volume_m3

    @property
    def faces(self):
        return list(self._faces)

    @property
    def volume(self):
        return FakeQuantity(self._volume)


class FakeComponent:
    def __init__(self, name, bodies=(), components=()):
        self.name = name
        self.bodies = list(bodies)
        self.components = list(components)
        self.named_selections = []


class FakeClient:
    """The modeler's gRPC client handle, as `Session.status` reads it."""

    backend_type = "BackendType.SPACECLAIM"

    def __init__(self, backend_version=(24, 2, 0, None, None), product_instance=None):
        self.backend_version = backend_version
        self._product_instance = product_instance


class FakeDesign(FakeComponent):
    is_active = True


class FakeRepairTools:
    """Records the *bodies* it was handed -- that is what the count bug was about."""

    def __init__(self, short_edges=None, small_faces=None):
        self.calls: list[tuple] = []
        self._short = short_edges or []
        self._small = small_faces or []

    def find_short_edges(self, bodies, length=0.0):
        self.calls.append(("find_short_edges", list(bodies), length))
        return list(self._short)

    def find_small_faces(self, bodies, area=None):
        self.calls.append(("find_small_faces", list(bodies), area))
        return list(self._small)

    def find_duplicate_faces(self, bodies):
        self.calls.append(("find_duplicate_faces", list(bodies)))
        return []

    def find_extra_edges(self, bodies):
        return []

    def find_missing_faces(self, bodies):
        return []

    def find_split_edges(self, bodies):
        return []

    def find_stitch_faces(self, bodies):
        return []

    def find_inexact_edges(self, bodies):
        return []


class FakeModeler:
    def __init__(self, design=None, script_result=None, script_error=None, client=None):
        self.client = client or FakeClient()
        self.design = design if design is not None else FakeDesign("Design1")
        self.repair_tools = FakeRepairTools()
        self.script_calls: list[dict] = []
        self._script_result = script_result or {}
        self._script_error = script_error
        self.create_design_calls: list[str] = []
        self.close_calls = 0

    def _materialise(self, values):
        """Add a body for every name the script says it created.

        The real service does this; the fake has to as well, otherwise the
        read-back step (`_describe_body`) would be tested against a design that
        never received anything and every success path would look like a failure.

        With ``design = None`` this is a no-op, which is how the attach case is
        modelled: the script created the body in the user's document and this
        client still cannot see it.
        """
        if self.design is None:
            return
        names = scdm._as_list(values.get("created"))
        if values.get("name"):
            names = names + [values["name"]]
        volume_m3 = None
        raw = values.get("volume_mm3")
        try:
            volume_m3 = float(raw) * 1e-9
        except (TypeError, ValueError):
            volume_m3 = None
        for name in names:
            if name in [b.name for b in self.design.bodies]:
                continue
            body = FakeBody(name, faces=4)
            if volume_m3 is not None:
                body._volume = volume_m3
            self.design.bodies.append(body)

    def run_script_file(self, file_path, script_args=None, import_design=False):
        self.script_calls.append(
            {
                "file_path": str(file_path),
                "args": script_args,
                "import_design": import_design,
                "text": open(file_path, encoding="utf-8").read(),
            }
        )
        if self._script_error is not None:
            if isinstance(self._script_error, dict):
                key = self._script_error.get("if_text_contains")
                if key and key in self.script_calls[-1]["text"]:
                    raise self._script_error["error"]
            else:
                raise self._script_error
        self._materialise(self._script_result)
        return dict(self._script_result), self.design

    def create_design(self, name):
        self.create_design_calls.append(name)
        return self.design

    def close(self):
        self.close_calls += 1


@pytest.fixture
def session(monkeypatch):
    """A live launch session with a fake modeler, ready for tool bodies."""
    s = scdm.Session()
    modeler = FakeModeler()
    s.modeler = modeler
    s.origin = "launch"
    s.design = modeler.design
    monkeypatch.setattr(server, "_session", s)
    return s


# ---------------------------------------------------------------------------
# 1. scdm_run_script: script_path vs script
# ---------------------------------------------------------------------------


def test_resolve_accepts_source_text_and_returns_a_temporary_file(tmp_path):
    path, temporary, source = scdm.resolve_script(script="result['x'] = '1'")

    assert temporary is True
    assert source == "script_text"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "result['x'] = '1'"
    path.unlink()


def test_resolve_accepts_a_path_and_does_not_copy_it(tmp_path):
    target = tmp_path / "probe.py"
    target.write_text("result = {}\n", encoding="utf-8")

    path, temporary, source = scdm.resolve_script(script_path=str(target))

    assert path == target
    assert temporary is False
    assert source == "script_path"


def test_resolve_refuses_both_arguments(tmp_path):
    target = tmp_path / "probe.py"
    target.write_text("pass\n", encoding="utf-8")

    with pytest.raises(scdm.SessionError) as info:
        scdm.resolve_script(script_path=str(target), script="pass")

    assert info.value.code == "ambiguous_script_argument"
    assert "not both" in str(info.value)


def test_resolve_refuses_neither_argument():
    with pytest.raises(scdm.SessionError) as info:
        scdm.resolve_script()

    assert info.value.code == "missing_script"


def test_missing_script_path_says_it_is_a_path():
    with pytest.raises(scdm.SessionError) as info:
        scdm.resolve_script(script_path=r"C:\nowhere\does_not_exist.py")

    assert info.value.code == "script_not_found"
    # The old failure echoed the whole script back; this one must say what it is.
    assert "PATH to an IronPython file" in str(info.value)
    assert "`script` parameter" in str(info.value)


def test_run_script_with_text_writes_a_temp_file_and_removes_it(session):
    modeler = session.modeler

    result = session.run_script(script="result['answer'] = '42'", import_design=False)

    assert result["script_source"] == "script_text"
    assert result["script_was_temporary"] is True
    assert len(modeler.script_calls) == 1
    assert "answer" in modeler.script_calls[0]["text"]
    # Removed afterwards: the temp file must not accumulate.
    import os

    assert not os.path.exists(modeler.script_calls[0]["file_path"])


def test_run_script_with_a_path_runs_that_file(tmp_path, session):
    target = tmp_path / "probe.py"
    target.write_text("result['answer'] = '7'\n", encoding="utf-8")

    result = session.run_script(script_path=str(target), import_design=False)

    assert result["script_source"] == "script_path"
    assert result["script_was_temporary"] is False
    assert session.modeler.script_calls[0]["file_path"] == str(target)


def test_server_run_script_has_both_parameters():
    parameters = inspect.signature(server.scdm_run_script).parameters

    assert "script_path" in parameters
    assert "script" in parameters
    # The old, misleading single parameter is gone.
    assert "script" in parameters and parameters["script"].default is None


def test_server_run_script_reports_the_structured_error(session):
    result = server.scdm_run_script(script_path=r"C:\nope\nope.py")

    assert result["ok"] is False
    assert result["error"] == "script_not_found"


# ---------------------------------------------------------------------------
# 2. scdm_export: the file name is the caller's
# ---------------------------------------------------------------------------


class FakeExporter:
    """Writes whatever the export method is asked for, into the given directory."""

    def __init__(self, name="设计(D)1.stp"):
        self.name = name
        self.calls: list[str] = []
        self.is_active = True

    def _write(self, location):
        from pathlib import Path

        self.calls.append(str(location))
        out = Path(location) / self.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 32)
        return out

    # One per format: the server dispatches on `export_to_<format>`.
    export_to_scdocx = _write
    export_to_step = _write
    export_to_iges = _write
    export_to_parasolid_text = _write
    export_to_parasolid_bin = _write
    export_to_pmdb = _write


def _export_session(monkeypatch, name="设计(D)1.stp"):
    s = scdm.Session()
    design = FakeExporter(name=name)
    s.design = design
    s.modeler = FakeModeler(design=design)
    s.origin = "launch"
    monkeypatch.setattr(server, "_session", s)
    return s, design


def test_export_reports_the_file_not_the_directory(tmp_path, monkeypatch):
    s, _ = _export_session(monkeypatch)

    result = s.export("step", location=str(tmp_path))

    from pathlib import Path

    assert Path(result["path"]).is_file()
    assert result["exists"] is True
    assert result["bytes"] == 32
    # The old version echoed the directory it was handed.
    assert Path(result["path"]).parent == tmp_path.resolve()


def test_export_output_dir_plus_stem_controls_the_name(tmp_path, monkeypatch):
    s, _ = _export_session(monkeypatch)

    result = s.export("step", output_dir=str(tmp_path), file_stem="pipe_round")

    from pathlib import Path

    final = Path(result["path"])
    assert final.name == "pipe_round.stp", result
    assert final.is_file()
    assert result["expected_suffix"] == ".stp"


def test_export_reports_the_real_suffix_for_each_format():
    """The extension is not the format name; `step` writes `.stp`."""
    assert scdm.EXPORT_SUFFIXES["step"] == ".stp"
    assert scdm.EXPORT_SUFFIXES["scdocx"] == ".scdocx"


def test_export_renames_even_when_the_service_picks_a_chinese_name(tmp_path, monkeypatch):
    s, _ = _export_session(monkeypatch, name="设计(D)1.stp")

    result = s.export("step", output_dir=str(tmp_path), file_stem="pipe")

    from pathlib import Path

    assert Path(result["path"]).name == "pipe.stp"
    assert not (tmp_path / "设计(D)1.stp").exists()


def test_export_requires_a_destination():
    s = scdm.Session()
    s.design = FakeExporter()
    s.modeler = FakeModeler(design=s.design)

    with pytest.raises(scdm.SessionError) as info:
        s.export("step")

    assert info.value.code == "missing_parameter"


def test_export_file_stem_without_dir_is_refused(tmp_path):
    s = scdm.Session()
    design = FakeExporter()
    s.design = design
    s.modeler = FakeModeler(design=design)

    with pytest.raises(scdm.SessionError) as info:
        s.export("step", location=str(tmp_path), file_stem="pipe")

    assert info.value.code == "invalid_parameter"
    assert "output_dir" in str(info.value)
    # The refusal happens before anything is written or created.
    assert design.calls == []
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. inspect_geometry counts: exactly the truth, no doubling
# ---------------------------------------------------------------------------


def _geometry_session(monkeypatch, design, repair=None):
    s = scdm.Session()
    modeler = FakeModeler(design=design)
    if repair is not None:
        modeler.repair_tools = repair
    s.modeler = modeler
    s.design = design
    s.origin = "launch"
    monkeypatch.setattr(server, "_session", s)
    return s


def test_body_index_keys_are_aliases_not_bodies():
    """The bug: `len(_body_index(...))` is a key count, not a body count."""
    body = FakeBody("stator", faces=4)
    design = FakeDesign("设计(D)1", components=[FakeComponent("asm", bodies=[body])])

    index = scdm._body_index(design)

    # Several ways to name it, one body.
    assert len(index) > 1
    assert len(scdm._unique_bodies(index)) == 1


def test_unique_bodies_does_not_merge_same_named_solids():
    """An imported assembly names every solid 实体; identity must be the key."""
    a = FakeBody("实体", faces=1)
    b = FakeBody("实体", faces=1)
    design = FakeDesign("Design1", components=[FakeComponent("asm", bodies=[a, b])])

    assert len(scdm._unique_bodies(scdm._body_index(design))) == 2


def test_inspect_geometry_counts_match_list_bodies(monkeypatch):
    body = FakeBody("PIPE", faces=4, volume_m3=1.413677228662212e-4)
    design = FakeDesign("设计(D)1", bodies=[body])
    s = _geometry_session(monkeypatch, design)

    described = s.describe(design)
    report = s.inspect_geometry()

    assert described["body_count"] == 1
    assert report["bodies_checked"] == 1, "the ×2 bug: 1 body reported as 2"
    assert report["faces_checked"] == 4, "the ×2 bug: 4 faces reported as 8"


def test_inspect_geometry_counts_a_722_face_body_as_one_body(monkeypatch):
    body = FakeBody("PIPE_D50", faces=722)
    design = FakeDesign("设计(D)1", components=[FakeComponent("PIPE", bodies=[body])])
    s = _geometry_session(monkeypatch, design)

    report = s.inspect_geometry()

    assert report["bodies_checked"] == 1
    assert report["faces_checked"] == 722


def test_inspect_geometry_passes_unique_bodies_to_the_kernel(monkeypatch):
    """Not just the reported count: the kernels must not be asked twice either."""
    body = FakeBody("stator", faces=3)
    design = FakeDesign("asm31", components=[FakeComponent("asm", bodies=[body])])
    repair = FakeRepairTools()
    s = _geometry_session(monkeypatch, design, repair)

    s.inspect_geometry(checks=["duplicate_faces"])

    name, handed, *_ = repair.calls[0]
    assert name == "find_duplicate_faces"
    assert handed == [body], "the same body was handed over more than once"


# ---------------------------------------------------------------------------
# 7. short_edges attribution
# ---------------------------------------------------------------------------


def test_classify_calls_uniform_short_edges_polygonal_chords():
    """The measured 360-gon: 1440 edges, 0.3491-0.4363 mm, two chord families."""
    lengths = [0.3491] * 720 + [0.4363] * 720

    result = scdm.classify_short_edges(lengths, curve_radius_mm=20.0)

    assert result["verdict"] == "polygonal_chords"
    assert result["span_ratio"] == pytest.approx(0.4363 / 0.3491, rel=1e-3)
    # Two families merged, so the CV is not zero -- 0.111, which is exactly why
    # the threshold cannot be a "near zero" test.
    assert result["coefficient_of_variation"] == pytest.approx(0.111, abs=0.005)
    # 0.3491 mm is the chord of a circle of radius 20 mm with 360 facets.
    assert 360 in result["facet_count_estimate"]
    assert "2*r*sin(pi/n)" in result["chord_formula"]
    assert "semicircle" in result["to_remove_this_class"]


def test_classify_says_it_cannot_estimate_without_the_radius():
    """`chord = 2*r*sin(pi/n)` has two unknowns; one of them must be supplied."""
    result = scdm.classify_short_edges([0.35] * 20)

    assert result["facet_count_estimate"] is None
    assert "cannot supply" in result["facet_count_note"]


def test_classify_calls_scattered_lengths_a_real_defect():
    lengths = [0.1, 0.05, 0.9, 0.02, 0.7, 0.3, 0.011, 0.55, 0.4, 0.06]

    result = scdm.classify_short_edges(lengths)

    assert result["verdict"] == "mixed_variation"
    assert "not one tessellation chord family" in result["why"]


def test_classify_declines_when_there_is_too_little_to_go_on():
    result = scdm.classify_short_edges([0.2, 0.2, 0.2])

    assert result["verdict"] == "undetermined"
    assert "too few" in result["why"]


def test_classify_with_no_edges_is_undetermined_not_clean():
    result = scdm.classify_short_edges([])

    assert result["verdict"] == "undetermined"
    assert result["measured"] == 0


def test_classify_declares_the_rule_it_applied():
    result = scdm.classify_short_edges([0.35] * 20)

    assert result["rule"]["min_count"] == scdm.POLYGON_CHORD_MIN_COUNT
    assert result["rule"]["max_span_ratio"] == scdm.POLYGON_CHORD_MAX_SPAN_RATIO
    assert result["rule"]["max_cv"] == scdm.POLYGON_CHORD_MAX_CV


def test_short_edges_check_carries_attribution(monkeypatch):
    edges = [FakeEdge(f"e{i}", 0.00035) for i in range(20)]
    repair = FakeRepairTools(short_edges=[FakeShortEdgeProblem("p1", edges)])
    body = FakeBody("pipe360", faces=722)
    design = FakeDesign("设计(D)1", bodies=[body])
    s = _geometry_session(monkeypatch, design, repair)

    report = s.inspect_geometry(checks=["short_edges"])

    attribution = report["checks"]["short_edges"]["attribution"]
    assert attribution["verdict"] == "polygonal_chords"
    assert attribution["measured"] == 20
    assert attribution["length_min_mm"] == pytest.approx(0.35, rel=1e-6)
    assert report["checks"]["short_edges"]["span"]["measured"] == 20


def test_short_edges_assessment_says_clean_is_not_a_defect(monkeypatch):
    edges = [FakeEdge(f"e{i}", 0.00035) for i in range(20)]
    repair = FakeRepairTools(short_edges=[FakeShortEdgeProblem("p1", edges)])
    body = FakeBody("pipe360", faces=722)
    design = FakeDesign("设计(D)1", bodies=[body])
    s = _geometry_session(monkeypatch, design, repair)

    report = s.inspect_geometry(checks=["short_edges"])

    # clean is still false -- the raw count is not hidden -- but it is explained.
    assert report["clean"] is False
    assert "not geometry defects" in report["assessment"]


def test_attribution_reports_unreadable_lengths(monkeypatch):
    class BrokenEdge(FakeEdge):
        @property
        def length(self):
            raise ValueError("The norm of the 3D vector is not valid.")

    edges = [FakeEdge("ok", 0.00035)] + [BrokenEdge(f"b{i}", 0.0) for i in range(20)]
    repair = FakeRepairTools(short_edges=[FakeShortEdgeProblem("p1", edges)])
    design = FakeDesign("设计(D)1", bodies=[FakeBody("b", faces=1)])
    s = _geometry_session(monkeypatch, design, repair)

    report = s.inspect_geometry(checks=["short_edges"])

    assert report["checks"]["short_edges"]["attribution"]["unreadable"] == 20


# ---------------------------------------------------------------------------
# 5. Kernel limitations are translated, not echoed
# ---------------------------------------------------------------------------


def test_acis_guidance_names_the_working_overload():
    exc = RuntimeError("AcisModeler::CreatePlanarBody Not implemented for ACIS.")

    text = scdm.acis_guidance(exc)

    assert text is not None
    assert "kernel" in text
    assert "not a parameter error" in text
    assert "CurveSegment.Create" in text
    assert "removeInnerRegions=True" in text


def test_acis_guidance_explains_the_full_circle_trap():
    text = scdm.acis_guidance(RuntimeError("Invalid profile."))

    assert text is not None
    assert "start == end" in text
    assert "two semicircles" in text


def test_acis_guidance_returns_none_for_an_unrelated_error():
    """A body called 'kernel_bracket' must not be relabelled a kernel limit."""
    assert scdm.acis_guidance(RuntimeError("body not found: kernel_bracket")) is None
    assert scdm.acis_guidance(ValueError("nope")) is None


def test_kernel_limitations_records_the_silent_one():
    text = scdm.KERNEL_LIMITATIONS["remove_inner_regions_false"]

    # The analytic values, not the modelling report's printed ones: the report had
    # 706.634 (digits transposed from 706.858) and 1962.872 (pi*25^2 is 1963.495).
    assert "1963.495" in text
    assert "706.858" in text
    assert "silently" in text


def test_run_script_translates_a_kernel_refusal(session):
    session.modeler._script_error = RuntimeError(
        "AcisModeler::CreatePlanarBody Not implemented for ACIS."
    )

    with pytest.raises(scdm.SessionError) as info:
        session.run_script(script="pass", import_design=False)

    assert info.value.code == "kernel_limitation"
    assert "not a parameter error" in str(info.value)


# ---------------------------------------------------------------------------
# 6. removeInnerRegions=False is caught by read-back
# ---------------------------------------------------------------------------


def test_ring_areas_match_the_analytic_values():
    """pi*25^2 = 1963.495 and pi*(25^2-20^2) = 706.858.

    Note what is NOT asserted here: the modelling report quoted 1962.872 mm2 for
    the outer disc and 706.634 mm2 for the annulus. Both are wrong -- the annulus
    figure has its first two digits transposed and the disc figure is off by
    ~0.6 mm2 -- so the guard compares against the analytic values instead.
    """
    params = scdm.ring_areas_mm2(50.0, 40.0)

    assert params["annulus_mm2"] == pytest.approx(706.858347, rel=1e-8)
    assert params["outer_disc_mm2"] == pytest.approx(1963.495408, rel=1e-8)
    assert params["wall_thickness_mm"] == pytest.approx(5.0)


def test_ring_areas_for_a_rod_are_the_full_disc():
    params = scdm.ring_areas_mm2(12.0)

    assert params["annulus_mm2"] == pytest.approx(113.0973355, rel=1e-8)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"outer_diameter_mm": 0, "inner_diameter_mm": 0},
        {"outer_diameter_mm": -5, "inner_diameter_mm": 0},
        {"outer_diameter_mm": 50, "inner_diameter_mm": 50},
        {"outer_diameter_mm": 50, "inner_diameter_mm": 60},
        {"outer_diameter_mm": 50, "inner_diameter_mm": -1},
        {"outer_diameter_mm": "50", "inner_diameter_mm": 0},
        {"outer_diameter_mm": 50, "inner_diameter_mm": "20"},
        {"outer_diameter_mm": True, "inner_diameter_mm": 0},
    ],
)
def test_ring_areas_rejects_impossible_dimensions(kwargs):
    with pytest.raises(scdm.SessionError) as info:
        scdm.ring_areas_mm2(**kwargs)

    assert info.value.code == "invalid_parameter"


def test_create_pipe_refuses_a_duplicate_name(monkeypatch):
    design = FakeDesign("Design1", bodies=[FakeBody("PIPE_OD50_ID40_L200")])
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.create_pipe(50.0, 200.0, 40.0)

    assert info.value.code == "name_conflict"
    assert "PIPE_OD50_ID40_L200" in str(info.value)
    assert s.modeler.script_calls == [], "nothing should have been run"


def test_create_pipe_reports_the_silent_lost_bore(monkeypatch):
    """The script raises SECTION_LOST_BORE; the caller must see why."""
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_error = RuntimeError(
        "SECTION_LOST_BORE: section area 1962.871940 mm2 equals the OUTER DISC "
        "pi*r_out^2 = 1962.871940 mm2, not the annulus 706.858347 mm2."
    )

    with pytest.raises(RuntimeError) as info:
        s.create_pipe(50.0, 200.0, 40.0)

    assert "SECTION_LOST_BORE" in str(info.value)


def test_server_create_pipe_reports_a_failed_guard_as_a_structured_error(monkeypatch):
    design = FakeDesign("Design1")
    _geometry_session(monkeypatch, design)
    server._session.modeler._script_error = RuntimeError("SECTION_AREA: bad")

    result = server.scdm_create_pipe(outer_diameter_mm=50.0, length_mm=200.0,
                                     inner_diameter_mm=40.0)

    assert result["ok"] is False
    assert result["error"] == "RuntimeError"
    assert "SECTION_AREA" in result["message"]


def test_create_pipe_refuses_when_the_script_returns_no_body(monkeypatch):
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {}

    with pytest.raises(scdm.SessionError) as info:
        s.create_pipe(50.0, 200.0, 40.0)

    assert info.value.code == "creation_failed"
    assert "Nothing was accepted" in str(info.value)


def test_create_pipe_verifies_the_area_it_read_back(monkeypatch):
    """A script that reports a disc area where the annulus belongs is refused."""
    design = FakeDesign("Design1", bodies=[FakeBody("PIPE_OD50_ID40_L200")])
    geometry = _geometry_session(monkeypatch, design)
    geometry.modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "1962.871940000",   # the silent disc result
        "volume_mm3": "392574.388000000",
        "faces": "4",
        "solid": "False",
    }

    with pytest.raises(scdm.SessionError) as info:
        geometry.create_pipe(50.0, 200.0, 40.0, replace_existing=True)

    assert info.value.code == "creation_verification_failed"
    assert info.value.extra["checks"]["section_area"]["ok"] is False
    assert info.value.extra["checks"]["volume"]["ok"] is False


def test_create_pipe_accepts_the_true_measured_values(monkeypatch):
    """The real 50/40/200 pipe: area 706.858347 mm2, volume 141367.722866 mm3.

    The body is pre-seeded with the name the tool will choose, so this also pins
    that `replace_existing=True` is what clears the way.
    """
    body = FakeBody("PIPE_OD50_ID40_L200", faces=4, volume_m3=1.413677228662212e-4)
    design = FakeDesign("设计(D)1", bodies=[body])
    geometry = _geometry_session(monkeypatch, design)
    geometry.modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "706.858347000",
        "volume_mm3": "141367.722866000",
        "faces": "4",
        "solid": "False",
    }

    result = geometry.create_pipe(50.0, 200.0, 40.0, replace_existing=True)

    assert result["created"]["name"] == "PIPE_OD50_ID40_L200"
    assert result["solid"] is False
    assert result["checks"]["section_area"]["ok"] is True
    # The volume residual is 2.8e-5 relative, inside the 1e-3 default tolerance.
    assert result["checks"]["volume"]["ok"] is True
    assert result["expected"]["annulus_mm2"] == pytest.approx(706.858347, rel=1e-8)


def test_create_pipe_generates_the_documented_route(monkeypatch):
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "706.858347",
        "volume_mm3": "141371.669412",
        "solid": "False",
    }

    s.create_pipe(50.0, 200.0, 40.0)

    text = s.modeler.script_calls[0]["text"]
    # The two calls the measured route needs, and no boolean anywhere.
    assert "Body.CreatePlanarBody(PLANE_BY_SIDE[SIDE], profile, False, False, True)" in text
    assert "ExtrudeType.ForceIndependent" in text
    assert "CurveSegment.CreateArc" in text, "circles must be real arcs, not a polygon"
    assert "removeInnerRegions=False" not in text
    assert "Body.Subtract" not in text and ".subtract(" not in text
    # Units: every length goes through MM().
    assert "MM(r_mm)" in text
    assert "MM(LENGTH_MM)" in text
    # Real Direction objects, never bare tuples and never System.Double[].
    # (`System.Double[]` still appears in a comment explaining why it is not used,
    # so the assertion is on the assignment form.)
    assert "Direction.Create" in text
    assert "= System.Double[]" not in text
    assert "\nimport System" not in text
    # Only the three static planes: Plane.Create cannot be constructed here (it
    # appears in a comment saying so, hence the assignment-form assertion).
    assert "Plane.PlaneXY" in text
    assert "= Plane.Create(" not in text


def test_create_pipe_script_has_the_dimensions_interpolated(monkeypatch):
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "706.858347",
        "volume_mm3": "141371.669412",
        "solid": "False",
    }

    s.create_pipe(50.0, 200.0, 40.0)

    text = s.modeler.script_calls[0]["text"]
    assert "R_OUT_MM = 25.0" in text
    assert "R_IN_MM = 20.0" in text
    assert "LENGTH_MM = 200.0" in text
    # The expected values come from the analytic formula, so the assertion is
    # self-checking rather than a transcription of whatever the code printed.
    expected = scdm.ring_areas_mm2(50.0, 40.0)["annulus_mm2"]
    assert f"EXPECTED_AREA_MM2 = {expected!r}" in text
    assert f"EXPECTED_VOLUME_MM3 = {expected * 200.0!r}" in text


def test_create_pipe_default_name_states_the_geometry(monkeypatch):
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "706.858347",
        "volume_mm3": "141371.669412",
        "solid": "False",
    }

    s.create_pipe(50.0, 200.0, 40.0)

    assert "PIPE_OD50_ID40_L200" in s.modeler.script_calls[0]["text"]


def test_pipe_name_marks_a_solid_as_a_rod():
    assert scdm._pipe_name(12.0, 0.0, 40.0) == "ROD_D12_L40"
    assert scdm._pipe_name(50.0, 40.0, 200.0) == "PIPE_OD50_ID40_L200"


# ---------------------------------------------------------------------------
# 5/6 continued: plane axes, and the no-design bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "normal",
    [(0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
     (0.5773502691896258, 0.5773502691896258, 0.5773502691896258), (-1.0, 0.0, 0.0)],
)
def test_plane_axes_cross_product_is_the_normal(normal):
    """`Plane(origin, dx, dy)` derives the normal, so dx x dy must be it."""
    _, dx, dy = scdm._plane_axes(None, normal)

    cross = (
        dx[1] * dy[2] - dx[2] * dy[1],
        dx[2] * dy[0] - dx[0] * dy[2],
        dx[0] * dy[1] - dx[1] * dy[0],
    )
    norm = sum(c * c for c in cross) ** 0.5
    unit = tuple(c / norm for c in cross)
    assert unit == pytest.approx(normal, abs=1e-6)


def test_plane_axes_rejects_a_bad_origin():
    with pytest.raises(scdm.SessionError) as info:
        scdm._plane_axes([0.0, 0.0], (0.0, 0.0, 1.0))

    assert info.value.code == "invalid_parameter"


@pytest.mark.parametrize("bad_axis", ["w", [1, 2], [0, 0, 0], [1, "2", 3], None, 5])
def test_unit_direction_vector_rejects_nonsense(bad_axis):
    with pytest.raises(scdm.SessionError):
        scdm._unit_direction_vector(bad_axis)


@pytest.mark.parametrize(
    "axis,expected",
    [("z", (0.0, 0.0, 1.0)), ("X", (1.0, 0.0, 0.0)), ([0, 0, 2], (0.0, 0.0, 1.0))],
)
def test_unit_direction_vector_normalises(axis, expected):
    assert scdm._unit_direction_vector(axis) == pytest.approx(expected)


def test_ensure_design_reports_no_design_for_reads(monkeypatch):
    s = scdm.Session()
    modeler = FakeModeler()

    def no_design():
        raise RuntimeError("There is no opened document.")

    modeler.get_active_design = no_design
    modeler.read_existing_design = no_design
    s.modeler = modeler
    s.origin = "attach"

    with pytest.raises(scdm.SessionError) as info:
        s._ensure_design(create=False)

    assert info.value.code == "no_design"
    assert modeler.create_design_calls == [], "a read must not create a document"


def test_ensure_design_creates_one_when_the_caller_asks(monkeypatch):
    """Measured: the user's session really can have no document open.

    A LAUNCH session -- the server owns the process, so bringing a document into
    existence is its own business.
    """
    s = scdm.Session()
    modeler = FakeModeler()

    def no_design():
        raise RuntimeError("There is no opened document.")

    modeler.get_active_design = no_design
    modeler.read_existing_design = no_design
    s.modeler = modeler
    s.origin = "launch"

    design = s._ensure_design(create=True)

    assert design is modeler.design
    assert modeler.create_design_calls == [scdm.CREATE_DESIGN_NAME]
    assert s.design is design


def test_ensure_design_refuses_to_create_on_an_attach_session():
    """The user's document may be the very thing create_design() closes."""
    s = scdm.Session()
    modeler = FakeModeler()

    def no_design():
        raise RuntimeError("There is no opened document.")

    modeler.get_active_design = no_design
    modeler.read_existing_design = no_design
    s.modeler = modeler
    s.origin = "attach"

    with pytest.raises(scdm.SessionError) as info:
        s._ensure_design(create=True)

    assert info.value.code == "attach_refuses_create_design"
    assert info.value.extra["process_untouched"] is True
    assert modeler.create_design_calls == [], "it must not have touched the document"
    assert "unsaved work" in str(info.value)


def test_visible_bodies_is_none_when_the_client_cannot_see_a_design():
    """None is not [] -- 'cannot look' must not read as 'nothing is there'."""
    s = scdm.Session()
    s.modeler = FakeModeler()

    assert s._visible_bodies() is None

    s.design = FakeDesign("Design1", bodies=[FakeBody("stator")])
    assert s._visible_bodies() == ["stator"]


def test_create_pipe_reports_name_conflict_from_the_script(monkeypatch):
    """When the client cannot see the design, the script makes the same check."""
    s = scdm.Session()
    s.origin = "attach"
    modeler = FakeModeler()
    s.modeler = modeler
    s.design = None
    monkeypatch.setattr(server, "_session", s)
    modeler._script_result = {
        "failed": (
            "name_conflict: a body named 'PIPE_X' already exists in the open document."
        )
    }

    with pytest.raises(scdm.SessionError) as info:
        s.create_pipe(50.0, 200.0, 40.0, name="PIPE_X")

    assert info.value.code == "name_conflict"


def test_create_pipe_reports_lost_bore_from_the_script(monkeypatch):
    design = FakeDesign("Design1")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "failed": "lost_bore: the section area 1963.49 mm2 equals the OUTER DISC"
    }

    with pytest.raises(scdm.SessionError) as info:
        s.create_pipe(50.0, 200.0, 40.0)

    assert info.value.code == "lost_bore"
    assert "OUTER DISC" in str(info.value)


def test_extrude_reports_body_not_found_from_the_script(monkeypatch):
    design = FakeDesign("Design1", bodies=[FakeBody("SOURCE")])
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {"failed": "body_not_found: no body named 'NOPE'"}

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, direction="z")

    assert info.value.code == "body_not_found"


def test_script_error_uses_a_fallback_code_for_an_unknown_tag():
    with pytest.raises(scdm.SessionError) as info:
        scdm._script_error("scdm_extrude", {"failed": "something new went wrong"})

    assert info.value.code == "scdm_extrude_failed"


def test_script_error_is_a_no_op_on_success():
    assert scdm._script_error("scdm_extrude", {"created": "['a']"}) is None
    assert scdm._script_error("scdm_extrude", {}) is None


def test_generated_scripts_report_failures_instead_of_raising():
    """The service collapses every script exception into 'Script run failed.'"""
    # The machinery lives in the shared prelude; each script calls it.
    assert "_guard(" in scdm._PRELUDE_TEMPLATE
    assert "result['failed']" in scdm._PRELUDE_TEMPLATE
    assert "type(e).__name__" in scdm._PRELUDE_TEMPLATE
    for template in (scdm._PIPE_SCRIPT, scdm._EXTRUDE_SCRIPT):
        assert "_guard(_body)" in template
        assert "_fail(" in template


def test_generated_pipe_script_tags_every_user_visible_failure():
    text = scdm._PIPE_SCRIPT

    for tag in ("name_conflict:", "lost_bore:", "section_area:", "volume:",
                "extrude_created_nothing:"):
        assert tag in text, tag


def test_create_pipe_reports_service_verification_when_the_design_is_invisible(monkeypatch):
    """An attach session: the guard runs, the second read cannot, and it says so."""
    s = scdm.Session()
    s.origin = "attach"
    modeler = FakeModeler()
    modeler.design = None  # this client sees no design, as measured on attach
    s.modeler = modeler
    s.design = None
    monkeypatch.setattr(server, "_session", s)

    modeler._script_result = {
        "name": "PIPE_OD50_ID40_L200",
        "section_area_mm2": "706.858347000",
        "volume_mm3": "141367.722866000",
        "faces": "4",
        "edges": "4",
        "solid": "False",
    }

    result = s.create_pipe(50.0, 200.0, 40.0)

    assert result["verification"] == "service"
    assert "cannot see the design" in result["verification_note"]
    assert result["checks"]["section_area"]["ok"] is True
    assert result["created"]["faces"] == 4, "the script's own read is still reported"


def test_ensure_design_never_stringifies_the_service_exception(monkeypatch):
    """Measured: `GeometryExitedError.__str__` re-raises, killing a naive handler."""
    s = scdm.Session()
    modeler = FakeModeler()

    class Exploding:
        def __str__(self):
            raise RuntimeError("str() re-raised")

    def no_design():
        raise Exploding()

    modeler.get_active_design = no_design
    modeler.read_existing_design = no_design
    s.modeler = modeler

    with pytest.raises(scdm.SessionError) as info:
        s._ensure_design(create=False)

    assert info.value.code == "no_design"


# ---------------------------------------------------------------------------
# 4. scdm_extrude
# ---------------------------------------------------------------------------


def _extrude_design(faces=1, surface="PLANE", name="SOURCE", volume_m3=1e-4):
    return FakeDesign(
        "Design1",
        bodies=[FakeBody(name, faces=faces, surface=surface, volume_m3=volume_m3)],
    )


def test_extrude_picks_only_planar_faces_by_default(monkeypatch):
    design = _extrude_design(faces=1, surface="PLANE")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
    }

    s.extrude("SOURCE", 40.0, direction="z")

    text = s.modeler.script_calls[0]["text"]
    assert "SOURCE" in text
    assert "ForceIndependent" in text
    assert "ExtrudeFaces.Execute" in text


def test_extrude_refuses_without_face_indices(monkeypatch):
    """No face kind can be read on this release, so the script refuses and lists them."""
    design = _extrude_design(faces=2, surface="CYLINDER")
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "failed": "no_extrudable_face: no face was selected, and this build cannot tell a "
        "planar face from a curved one. Faces on 'SOURCE': idx=0 area_mm2=1.0 ..."
    }

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, direction="x")

    assert info.value.code == "no_extrudable_face"
    assert "cannot tell a planar face" in str(info.value)


def test_extrude_refuses_an_unknown_body_and_lists_candidates(monkeypatch):
    design = _extrude_design()
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("NOPE", 10.0, direction="x")

    assert info.value.code == "body_not_found"
    assert "SOURCE" in info.value.extra["candidates"]


def test_extrude_reports_an_out_of_range_face_index(monkeypatch):
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {"failed": "face_not_found: face index 9 is out of range"}

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, direction="x", face_indices=[9])

    assert info.value.code == "face_not_found"


@pytest.mark.parametrize("bad", ["nope", [1, "2"], [True], 0, "0"])
def test_extrude_rejects_non_integer_face_indices(monkeypatch, bad):
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, direction="x", face_indices=bad)

    assert info.value.code == "invalid_parameter"
    assert s.modeler.script_calls == [], "a bad parameter must not reach the service"


def test_extrude_requires_a_direction(monkeypatch):
    """A face's normal cannot be read here, so guessing an axis is not allowed."""
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, face_indices=[0])

    assert info.value.code == "no_direction"
    assert s.modeler.script_calls == []


def test_extrude_reports_no_geometry_change_when_nothing_is_created(monkeypatch):
    """The measured shape of a declined extrusion: normal return, no body."""
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {"created": "[]"}

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 10.0, direction="z")

    assert info.value.code == "no_geometry_change"
    assert "Do not retry" in str(info.value)


def test_extrude_rejects_a_zero_distance(monkeypatch):
    design = _extrude_design()
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 0.0, direction="z")

    assert info.value.code == "invalid_parameter"


def test_extrude_checks_a_supplied_expected_volume(monkeypatch):
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
    }

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 40.0, direction="z", expected_volume_mm3=9999.0)

    assert info.value.code == "creation_verification_failed"
    assert info.value.extra["check"]["ok"] is False


def test_extrude_reports_a_cross_check_without_enforcing_it(monkeypatch):
    """area x distance is evidence, not a specification."""
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
    }

    result = s.extrude("SOURCE", 40.0, direction="z")

    assert result["check"]["ok"] is None
    assert result["check"]["expected_from_face_area_mm3"] == pytest.approx(4523.89342, rel=1e-6)
    assert result["check"]["relative_error_vs_face_area"] < 1e-4


def test_extrude_says_the_source_is_untouched(monkeypatch):
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
        "source_survived": "True",
        "source_volume_before_mm3": "100000.0",
        "source_volume_after_mm3": "100000.0",
    }

    result = s.extrude("SOURCE", 40.0, direction="z")

    assert result["source_untouched"] is True
    assert result["source_effect"]["volume_delta_mm3"] == 0.0
    assert "25R2" in result["route"]
    assert "EMPTY" in result["route"]


def test_extrude_reports_a_fused_pull_as_a_source_change(monkeypatch):
    """Add / ForceAdd move the source's own volume; "untouched" would be a lie."""
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "235.619449019",
        "volume_mm3": "1178.097245",
        "source_survived": "True",
        "source_volume_before_mm3": "7068.394036",
        "source_volume_after_mm3": "8246.459709",
        "extrude_type": "Add",
    }

    result = s.extrude("SOURCE", 5.0, direction="z", extrude_type="Add")

    assert result["source_untouched"] is False
    assert result["source_effect"]["volume_delta_mm3"] == pytest.approx(1178.065673, rel=1e-6)
    assert result["extrude_type"] == "Add"


def test_extrude_accepts_a_fused_pull_as_the_effect(monkeypatch):
    """Measured: ForceIndependent fused into a hollow pipe (+1178.08 mm3, no new body).

    That is a real effect and must not be reported as `no_geometry_change`; the
    created body is the target itself, and the volume is the source's own delta
    rather than the whole body.
    """
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE']",
        "created_count": "1",
        "fused_into_source": "True",
        "face_area_mm2": "235.619449019",
        "volume_mm3": "1178.081918915",
        "source_survived": "True",
        "source_volume_before_mm3": "14529.692374859",
        "source_volume_after_mm3": "15707.774293774",
        "extrude_type": "ForceIndependent",
    }

    result = s.extrude("SOURCE", 5.0, direction="z", face_indices=[2])

    assert result["created"][0]["name"] == "SOURCE"
    assert result["source_effect"]["fused_into_source"] is True
    assert result["source_untouched"] is False
    assert result["check"]["measured_volume_mm3"] == pytest.approx(1178.081918915, rel=1e-9)
    # The cross-check against face area x distance agrees, and is NOT enforced.
    assert result["check"]["ok"] is None
    assert result["check"]["relative_error_vs_face_area"] < 1e-4


def test_extrude_source_untouched_is_none_when_the_script_could_not_say(monkeypatch):
    """Unknown must not read as unchanged."""
    design = _extrude_design(faces=1, volume_m3=1e-4)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
    }

    result = s.extrude("SOURCE", 40.0, direction="z")

    assert result["source_untouched"] is None


def test_extrude_rejects_an_unknown_extrude_type(monkeypatch):
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)

    with pytest.raises(scdm.SessionError) as info:
        s.extrude("SOURCE", 1.0, direction="z", extrude_type="Subtract")

    assert info.value.code == "invalid_parameter"
    assert s.modeler.script_calls == []


# ---------------------------------------------------------------------------
# Small helpers the fixes rely on
# ---------------------------------------------------------------------------


def test_as_list_parses_what_run_script_file_returns():
    assert scdm._as_list("['a', 'b']") == ["a", "b"]
    assert scdm._as_list(["a"]) == ["a"]
    assert scdm._as_list("") == []
    assert scdm._as_list("not a list") == []
    assert scdm._as_list("[]") == []


def test_within_treats_an_unreadable_value_as_not_passing():
    assert scdm._within(None, 100.0, 0.01) is False
    assert scdm._within(100.5, 100.0, 0.01) is True
    assert scdm._within(101.5, 100.0, 0.01) is False


def test_safe_flag_returns_none_when_it_cannot_be_read():
    class Thing:
        @property
        def is_surface(self):
            raise RuntimeError("nope")

    assert scdm._safe_flag(Thing(), "is_surface") is None
    assert scdm._safe_flag(FakeBody("b"), "is_surface") is None


def test_prelude_pins_the_units_and_discovers_the_api_root():
    template = scdm._PRELUDE_TEMPLATE

    assert "LoadSCDMAPIModuleV242.py" in template
    assert "{scdm_root}" in template
    assert "DesignBody.Create(root, name, raw)" in template
    # `result` must exist before anything can fail, or a failing script returns
    # nothing and the caller sees only "Script run failed.". It is written `{{}}`
    # because the template is interpolated with str.format.
    assert "result = {{}}" in template
    assert template.index("result = {{}}") < template.index("root = DocumentHelper")
    rendered = template.format(scdm_root="X")
    assert "result = {}" in rendered


def test_script_root_is_discovered_not_hard_coded(monkeypatch):
    monkeypatch.setattr(
        scdm,
        "detect_ansys_roots",
        lambda: {"newest": "251", "spaceclaim": {"251": r"D:\ansys\v251\scdm\SpaceClaim.exe"}},
    )

    assert scdm._scdm_script_root() == r"D:\ansys\v251\scdm"


# ---------------------------------------------------------------------------
# Bug A: the three body counts must agree
# ---------------------------------------------------------------------------


def test_session_status_body_count_matches_list_bodies(monkeypatch):
    """Measured bug: the document had 1 body, `scdm_list_bodies` said 1, and
    `scdm_session_status` said 0. Same class as the x2 count bug."""
    body = FakeBody("PIPE", faces=4)
    design = FakeDesign("设计(D)1", bodies=[body])
    s = scdm.Session()
    s.modeler = FakeModeler(design=design)
    s.design = design
    s.origin = "launch"
    monkeypatch.setattr(server, "_session", s)

    status = server.scdm_session_status()["session"]
    listed = server.scdm_list_bodies()

    assert status["body_count"] == 1
    assert listed["body_count"] == 1
    assert status["body_count"] == listed["body_count"]


def test_all_three_body_counts_agree_across_a_tree(monkeypatch):
    """The invariant: status, list_bodies and inspect must not contradict each other.

    A nested component is what used to break it: the alias table holds several keys
    per body, so a count taken from `len(_body_index(...))` came out at 2x.
    """
    bodies = [FakeBody("stator", faces=4), FakeBody("pip", faces=6)]
    design = FakeDesign(
        "asm31",
        components=[FakeComponent("定转子装配", bodies=bodies), FakeComponent("单件", bodies=[FakeBody("inlet", faces=2)])],
    )
    s = _geometry_session(monkeypatch, design)
    monkeypatch.setattr(server, "_session", s)

    status = server.scdm_session_status()["session"]
    listed = server.scdm_list_bodies()
    inspected = s.inspect_geometry()

    assert status["body_count"] == 3
    assert listed["body_count"] == 3
    assert inspected["bodies_checked"] == 3
    assert status["body_count"] == listed["body_count"] == inspected["bodies_checked"]


def test_session_status_prefers_the_pinned_design(monkeypatch):
    """`self.design` and `modeler.design` can be different objects; the pinned one wins."""
    pinned = FakeDesign("pinned", bodies=[FakeBody("a"), FakeBody("b")])
    modeler_side = FakeDesign("other", bodies=[FakeBody("c")])
    s = scdm.Session()
    s.modeler = FakeModeler(design=modeler_side)
    s.design = pinned
    s.origin = "launch"

    status = s.status()

    assert status["design_name"] == "pinned"
    assert status["body_count"] == 2
    assert status["design_visible"] is True


def test_session_status_body_count_is_none_when_no_design_is_visible(monkeypatch):
    """0 would be a number that looks like a reading. Measured attach behaviour."""
    s = scdm.Session()
    modeler = FakeModeler()
    modeler.design = None
    s.modeler = modeler
    s.design = None
    s.origin = "attach"

    status = s.status()

    assert status["body_count"] is None
    assert status["design_visible"] is False
    assert "unknown rather than zero" in status["body_count_note"]


def test_describe_marks_an_invisible_design_apart_from_an_empty_one():
    s = scdm.Session()
    modeler = FakeModeler()
    modeler.design = None
    s.modeler = modeler
    s.design = None

    invisible = s.describe()
    assert invisible["design_visible"] is False
    assert "NOT 'the document is empty'" in invisible["note"]

    s.design = FakeDesign("Design1")
    empty = s.describe()
    assert empty["design_visible"] is True
    assert empty["body_count"] == 0
    assert empty["bodies"] == []


def test_status_without_a_session_still_has_no_design_fields(monkeypatch):
    """The disconnected shape must stay as it was."""
    status = scdm.Session().status()

    assert status["connected"] is False
    assert "body_count" not in status


# ---------------------------------------------------------------------------
# Bug B: export must not answer with a misleading no_design
# ---------------------------------------------------------------------------


def test_export_without_a_visible_design_names_the_real_situation():
    """Measured: IGES and Parasolid answered `no_design` while STEP succeeded.

    The bridge has no format-specific code path for any of them, so the misleading
    part is the message, and that is what changed.
    """
    s = scdm.Session()
    modeler = FakeModeler()
    modeler.design = None
    s.modeler = modeler
    s.design = None
    s.origin = "attach"

    with pytest.raises(scdm.SessionError) as info:
        s.export("iges", location=r"C:\tmp")

    assert info.value.code == "attach_export_needs_visible_design"
    assert info.value.code != "no_design"
    assert info.value.extra["process_untouched"] is True
    assert info.value.extra["next_step"] == "scdm_run_script"
    message = str(info.value)
    assert "NOT an empty session" in message
    assert "NOT format-specific" in message
    assert "STEP" in message


def test_export_validates_the_format_before_the_session(monkeypatch):
    """An unknown format is a parameter error whatever the session looks like."""
    s = scdm.Session()
    modeler = FakeModeler()
    modeler.design = None
    s.modeler = modeler
    s.design = None

    with pytest.raises(scdm.SessionError) as info:
        s.export("dwg", location=r"C:\tmp")

    assert "unsupported format" in str(info.value)
    assert "iges" in str(info.value)


def test_server_export_reports_the_structured_export_error():
    s = scdm.Session()
    modeler = FakeModeler()
    modeler.design = None
    s.modeler = modeler
    s.design = None
    s.origin = "attach"
    import ansys_bridge_mcp.server as srv

    original = srv._session
    try:
        srv._session = s
        result = srv.scdm_export(format="iges", location=r"C:\tmp")
        assert result["ok"] is False
        assert result["error"] == "attach_export_needs_visible_design"
        assert "STEP" in result["message"]
    finally:
        srv._session = original


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_the_new_tools_are_registered_and_are_plain_callables():
    assert inspect.isfunction(server.scdm_create_pipe)
    assert inspect.isfunction(server.scdm_extrude)
    assert inspect.isfunction(server.scdm_run_script)
    assert inspect.isfunction(server.scdm_export)


def test_create_pipe_signature_defaults():
    parameters = inspect.signature(server.scdm_create_pipe).parameters

    assert parameters["inner_diameter_mm"].default == 0.0
    assert parameters["axis"].default == "z"
    assert parameters["replace_existing"].default is False
    assert "outer_diameter_mm" in parameters and "length_mm" in parameters


_PIPE_SENTINELS = {
    "name": "zzNAME",
    "r_out_mm": 1.25,
    "r_in_mm": 0.5,
    "length_mm": 2.5,
    "dx": 0.0,
    "dy": 0.0,
    "dz": 1.0,
    "bx": 0.0,
    "by": 0.0,
    "bz": 0.0,
    "side": "zz",
    "expected_area": 0.75,
    "expected_volume": 1.875,
    "tolerance": 0.001,
    "replace": False,
}

_EXTRUDE_SENTINELS = {
    "source": "zzSOURCE",
    "face_indices": [0],
    "extrude_type": "zzOP",
    "dx": 0.0,
    "dy": 0.0,
    "dz": 1.0,
    "distance_mm": 2.5,
    "name": "zzNEW",
}


@pytest.mark.parametrize(
    "template,sentinel",
    [
        (scdm._PIPE_SCRIPT, "zzNAME"),
        (scdm._PIPE_SCRIPT, "zz"),
        (scdm._EXTRUDE_SCRIPT, "zzSOURCE"),
        (scdm._EXTRUDE_SCRIPT, "zzOP"),
        (scdm._EXTRUDE_SCRIPT, "zzNEW"),
    ],
)
def test_generated_scripts_do_not_double_quote_their_placeholders(template, sentinel):
    """`{name!r}` inside quotes gives `''name''` -- a syntax error that kills the script.

    Not hypothetical: it produced `SIDE = ''xy''` and `OP = ''ForceIndependent''`, and
    because a script syntax error happens before the failure-reporting dict can be
    written, the caller saw only "Script run failed." for both. No live session is
    needed to catch it.
    """
    values = _PIPE_SENTINELS if template is scdm._PIPE_SCRIPT else _EXTRUDE_SENTINELS
    rendered = template.format(**values)

    assert f"''{sentinel}" not in rendered
    assert f'""{sentinel}' not in rendered
    assert f"'{sentinel}" in rendered


def test_generated_scripts_format_without_missing_placeholders():
    """A template naming a key the caller does not pass raises KeyError here.

    The call itself is the assertion: `str.format` fails on an unknown field name, so
    a template that grew a placeholder nobody passes cannot reach a live session.
    """
    for template, values in (
        (scdm._PIPE_SCRIPT, _PIPE_SENTINELS),
        (scdm._EXTRUDE_SCRIPT, _EXTRUDE_SENTINELS),
    ):
        rendered = template.format(**values)
        assert rendered
        # Every declared placeholder was substituted, so no `{name}` survives with a
        # sentinel still inside it.
        assert "zzNAME" in rendered or "zzSOURCE" in rendered

    with pytest.raises(KeyError):
        scdm._PIPE_SCRIPT.format(**{k: v for k, v in _PIPE_SENTINELS.items() if k != "name"})


@pytest.mark.parametrize(
    "template,values",
    [
        (scdm._PIPE_SCRIPT, _PIPE_SENTINELS),
        (scdm._EXTRUDE_SCRIPT, _EXTRUDE_SENTINELS),
    ],
)
def test_generated_scripts_call_the_guard_exactly_once(template, values):
    """`_guard(_body)()` calls None and kills the script before it can report why.

    That is not hypothetical either: the extrude template had the extra `()`, and it
    made every extrusion fail with a bare "Script run failed." while the pipe template
    (which never had it) worked. `_guard` returns nothing, so the extra call raises
    inside the module body -- outside any handler.
    """
    rendered = template.format(**values)
    stripped = rendered.rstrip()

    assert stripped.endswith("_guard(_body)")
    assert "_guard(_body)()" not in rendered
    assert rendered.count("_guard(_body)") == 1


def test_server_extrude_passes_an_extrude_type(monkeypatch):
    design = _extrude_design(faces=1)
    s = _geometry_session(monkeypatch, design)
    s.modeler._script_result = {
        "created": "['SOURCE_EXTRUDED']",
        "face_area_mm2": "113.097335500",
        "volume_mm3": "4523.893420000",
        "source_survived": "True",
    }

    result = server.scdm_extrude(body="SOURCE", distance_mm=5.0, direction="z",
                                 face_indices=[0], extrude_type="ForceAdd")

    assert result["ok"] is True
    assert result["extrude_type"] == "ForceAdd"
    assert "ForceAdd" in s.modeler.script_calls[0]["text"]


def test_generated_extrude_script_tags_every_user_visible_failure():
    text = scdm._EXTRUDE_SCRIPT

    for tag in ("body_not_found:", "face_not_found:", "no_extrudable_face:"):
        assert tag in text, tag


def test_extrude_signature_defaults():
    parameters = inspect.signature(server.scdm_extrude).parameters

    assert parameters["direction"].default is None
    assert parameters["face_indices"].default is None
    assert parameters["expected_volume_mm3"].default is None


def test_export_signature_keeps_location_for_compatibility():
    parameters = inspect.signature(server.scdm_export).parameters

    assert parameters["location"].default is None
    assert parameters["output_dir"].default is None
    assert parameters["file_stem"].default is None


def test_tools_without_a_session_report_the_session_error():
    fresh = scdm.Session()
    import ansys_bridge_mcp.server as srv

    original = srv._session
    try:
        srv._session = fresh
        result = srv.scdm_create_pipe(outer_diameter_mm=10.0, length_mm=10.0)
        assert result["ok"] is False
        assert "no SpaceClaim session" in result["message"]
    finally:
        srv._session = original
