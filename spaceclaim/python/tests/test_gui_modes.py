"""Offline tests for the two GUI modes: launch (AI controls) and attach (user controls).

Everything here runs without ANSYS, without a licence and without a network call.
The PyAnsys client is replaced by a fake whose only job is to record what it was
asked to do -- which is exactly what these tests are about, because the substance
of the feature is *which* calls the server is willing to make:

* launch must ask for a visible window by default, and report truthfully whether it
  got one;
* attach must pass ``transport_mode`` and must NOT pass ``product_instance``, since
  that field is what decides whether the library can shut the backend down
  (``connection/client.py:493-499``);
* closing a user-owned session must be refused **before** the library is reached,
  and the refusal must carry the facts (``process_untouched``,
  ``licence_released``, ``next_step``) rather than only an apology;
* a failed attach must name the real cause (a double-click start has no gRPC
  endpoint) and hand over a command line the user can paste.

The last group calls the MCP tool functions directly. That is legitimate here
because FastMCP's ``@mcp.tool`` decorator returns the original function, verified
by ``tests/test_gui_modes.py::test_tools_are_plain_callables``: if a future
version of FastMCP wraps it instead, that test fails loudly rather than letting
these tests quietly stop covering the server layer.
"""

from __future__ import annotations

import inspect

import pytest

from ansys_bridge_mcp import scdm, server


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDesign:
    name = "Design1"
    is_active = True
    bodies: tuple = ()
    components: tuple = ()
    named_selections: tuple = ()


class FakeClient:
    def __init__(
        self,
        backend_type: str = "BackendType.SPACECLAIM",
        backend_version=(24, 2, 0, None, None),
        product_instance=None,
    ) -> None:
        self.backend_type = backend_type
        self.backend_version = backend_version
        self._product_instance = product_instance


class FakeModeler:
    """Records whether anything tried to close it. That is the whole point."""

    def __init__(self, client: FakeClient | None = None) -> None:
        self.client = client or FakeClient()
        self.design = FakeDesign()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeAGC:
    """Stands in for ``ansys.geometry.core``. Records the constructor arguments."""

    def __init__(self, modeler: FakeModeler | None = None, error: Exception | None = None) -> None:
        self.launch_calls: list[dict] = []
        self.modeler_calls: list[dict] = []
        self._modeler = modeler
        self._error = error

    def launch_modeler_with_spaceclaim(self, **kwargs) -> FakeModeler:
        self.launch_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._modeler or FakeModeler()

    def Modeler(self, **kwargs) -> FakeModeler:  # noqa: N802 - mirrors the library name
        self.modeler_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._modeler or FakeModeler()


@pytest.fixture
def fake(monkeypatch) -> FakeAGC:
    agc = FakeAGC()
    monkeypatch.setattr(scdm, "_import_geometry", lambda: agc)
    return agc


@pytest.fixture
def session(fake) -> scdm.Session:
    return scdm.Session()


@pytest.fixture
def live(monkeypatch, fake):
    """A Session plus the server bound to it, so tool functions can be called."""
    s = scdm.Session()
    monkeypatch.setattr(server, "_session", s)
    return s, fake


# ---------------------------------------------------------------------------
# Launch mode
# ---------------------------------------------------------------------------


def test_start_defaults_to_a_visible_window(session, fake):
    """hidden=False by default, matching the Fluent side's default ui_mode=gui."""
    session.start()

    assert len(fake.launch_calls) == 1
    assert fake.launch_calls[0]["hidden"] is False


def test_start_reports_the_mode_and_the_measured_visibility(session, fake):
    status = session.start()

    assert status["mode"] == "launch"
    assert status["ui_visible"] is True


def test_start_hidden_true_reports_ui_visible_false(session, fake):
    """hidden=True is /Headless: no UI at all, and the report must say so."""
    status = session.start(hidden=True)

    assert fake.launch_calls[0]["hidden"] is True
    assert status["mode"] == "launch"
    assert status["ui_visible"] is False


def test_start_passes_version_and_timeout_through(session, fake):
    session.start(version="242", hidden=False, timeout=42)

    call = fake.launch_calls[0]
    assert call["version"] == "242"
    assert call["timeout"] == 42


def test_start_records_backend_version_without_trailing_nones(session, fake):
    status = session.start()

    assert status["backend_version"] == "24.2.0"


def test_start_on_a_live_session_does_not_launch_a_second_process(session, fake):
    session.start()
    again = session.start(hidden=True)

    assert len(fake.launch_calls) == 1
    # Truthful, not wishful: the live session is still the visible one.
    assert again["ui_visible"] is True


def test_launch_status_carries_licence_and_ownership(session, fake):
    status = session.start()

    assert status["licence"]["new_licence_consumed"] is True
    assert status["process_ownership"]["owner"] == "ai"
    assert status["process_ownership"]["tool_will_close_it"] is True
    assert status["process_ownership"]["close_tool"] == "scdm_session_close"


def test_status_without_a_session_reports_no_mode(monkeypatch):
    status = scdm.Session().status()

    assert status["connected"] is False
    assert status["mode"] is None
    assert status["ui_visible"] is None


def test_close_on_a_launch_session_closes_it_and_releases_the_licence(session, fake):
    session.start()
    result = session.close()

    assert result["closed"] is True
    assert result["mode"] == "launch"
    assert result["licence_released"] is True
    assert session.connected is False
    assert session.mode is None


# ---------------------------------------------------------------------------
# Attach mode
# ---------------------------------------------------------------------------


def test_attach_passes_transport_mode_and_omits_product_instance(session, fake):
    """product_instance must stay unset: it is what decides who owns the process."""
    session.attach(host="localhost", port=51790, transport_mode="wnua", timeout=30)

    assert len(fake.modeler_calls) == 1
    call = fake.modeler_calls[0]
    assert call["host"] == "localhost"
    assert call["port"] == 51790
    assert call["transport_mode"] == "wnua"
    assert "product_instance" not in call
    # And it must not have gone anywhere near the launching entry point.
    assert fake.launch_calls == []


def test_attach_reports_mode_licence_ownership_and_observed(session, fake):
    status = session.attach()

    assert status["mode"] == "attach"
    assert status["licence"]["new_licence_consumed"] is False
    assert status["process_ownership"]["owner"] == "user"
    assert status["process_ownership"]["tool_will_close_it"] is False
    assert status["process_ownership"]["disconnect_tool"] == "scdm_detach"
    observed = status["observed"]
    assert observed["backend_type"] == "BackendType.SPACECLAIM"
    assert observed["backend_version"] == "24.2.0"
    # The fake modeler was built by Modeler(host, port), so it owns nothing.
    assert observed["has_product_instance"] is False
    assert isinstance(observed["connect_seconds"], float)


def test_attach_ui_visible_is_unknown_not_guessed(session, fake):
    """The user's window is not ours to observe, so the answer is None."""
    status = session.attach()

    assert status["ui_visible"] is None


def test_attach_records_where_it_attached(session, fake):
    status = session.attach(host="127.0.0.1", port="51800", transport_mode="WNUA")

    assert status["attached_to"] == {
        "host": "127.0.0.1",
        "port": 51800,
        "transport_mode": "wnua",
    }


def test_has_product_instance_is_true_when_the_client_owns_the_backend(fake, monkeypatch):
    fake = FakeAGC(FakeModeler(client=FakeClient(product_instance=object())))
    monkeypatch.setattr(scdm, "_import_geometry", lambda: fake)

    status = scdm.Session().attach()

    assert status["observed"]["has_product_instance"] is True


def test_attach_refuses_while_a_session_is_already_live(session, fake):
    session.start()

    with pytest.raises(scdm.SessionError) as info:
        session.attach()

    assert info.value.code == "session_already_connected"
    assert info.value.extra["next_step"] == "scdm_session_close"
    assert fake.modeler_calls == []


def test_attach_refuses_while_an_attach_session_is_already_live(session, fake):
    session.attach()

    with pytest.raises(scdm.SessionError) as info:
        session.attach()

    assert info.value.extra["next_step"] == "scdm_detach"


# -- attach: the failure the user will actually hit -------------------------


def test_attach_failure_names_the_double_click_and_gives_a_runnable_command(monkeypatch):
    fake = FakeAGC(error=RuntimeError("StatusCode.UNAVAILABLE"))
    monkeypatch.setattr(scdm, "_import_geometry", lambda: fake)

    with pytest.raises(scdm.SessionError) as info:
        scdm.Session().attach()

    assert info.value.code == "attach_unreachable"
    message = str(info.value)
    # The cause, in the words of the measurement.
    assert "double-click" in message
    assert "NO gRPC endpoint" in message
    assert "UNAVAILABLE" in message
    # The remedy, as something the user can paste.
    assert "set API_PORT=51790" in message
    assert scdm.SPACECLAIM_ADDIN_ARG in message
    assert "Presentation.ApiServerAddIn.Manifest.xml" in message
    assert f"{scdm.SPACECLAIM_TRANSPORT_ARG}wnua" in message
    assert "SpaceClaim.exe" in message


def test_attach_guidance_carries_the_users_actual_port_and_mode():
    text = scdm.attach_guidance("localhost", 51999, "insecure", RuntimeError("nope"))

    assert "set API_PORT=51999" in text
    assert f"{scdm.SPACECLAIM_TRANSPORT_ARG}insecure" in text


def test_attach_guidance_states_the_semi_public_risk():
    """The one thing a reader must not miss: this switch is not documented."""
    text = scdm.attach_guidance("localhost", 51790, "wnua")

    assert "SEMI-PUBLIC" in text
    assert "product_instance.py" in text
    assert "NOT in the public Ansys command-line documentation" in text


def test_attach_wrong_backend_is_refused_and_the_channel_is_handed_back(monkeypatch):
    modeler = FakeModeler(client=FakeClient(backend_type="BackendType.DISCOVERY"))
    fake = FakeAGC(modeler=modeler)
    monkeypatch.setattr(scdm, "_import_geometry", lambda: fake)

    session = scdm.Session()
    with pytest.raises(scdm.SessionError) as info:
        session.attach()

    assert info.value.code == "wrong_backend"
    assert info.value.extra["process_untouched"] is True
    # The only close() that ran was the cleanup of a refused connection.
    assert modeler.close_calls == 1
    assert session.connected is False


def test_attach_cleanup_failure_does_not_mask_the_refusal(monkeypatch):
    class Exploding(FakeModeler):
        def close(self) -> None:
            raise RuntimeError("channel already gone")

    modeler = Exploding(client=FakeClient(backend_type="BackendType.DISCOVERY"))
    monkeypatch.setattr(scdm, "_import_geometry", lambda: FakeAGC(modeler=modeler))

    with pytest.raises(scdm.SessionError) as info:
        scdm.Session().attach()

    assert info.value.code == "wrong_backend"


# -- attach: parameter validation, before any network call -----------------


@pytest.mark.parametrize("bad_port", [0, 65536, -1, "abc", "", None, True, 3.7, [51790]])
def test_attach_rejects_a_bad_port_without_connecting(session, fake, bad_port):
    with pytest.raises(scdm.SessionError) as info:
        session.attach(port=bad_port)

    assert info.value.code == "invalid_port"
    assert fake.modeler_calls == []
    assert session.connected is False


@pytest.mark.parametrize("good_port", [51790, "51790", 1, 65535, " 51800 "])
def test_attach_accepts_integer_and_numeric_string_ports(session, fake, good_port):
    session.attach(port=good_port)

    assert len(fake.modeler_calls) == 1
    assert isinstance(fake.modeler_calls[0]["port"], int)


@pytest.mark.parametrize("bad_mode", ["tls", "insecure-", "", None, 42, "wnua "])
def test_attach_rejects_an_unknown_transport_mode_without_connecting(session, fake, bad_mode):
    # "wnua " strips to a valid mode; everything else here must be refused.
    if isinstance(bad_mode, str) and bad_mode.strip() in scdm.VALID_TRANSPORT_MODES:
        session.attach(transport_mode=bad_mode)
        assert len(fake.modeler_calls) == 1
        return

    with pytest.raises(scdm.SessionError) as info:
        session.attach(transport_mode=bad_mode)

    assert info.value.code == "invalid_transport_mode"
    assert fake.modeler_calls == []


@pytest.mark.parametrize("bad_host", ["", "   ", None, 5])
def test_attach_rejects_a_blank_or_non_string_host(session, fake, bad_host):
    with pytest.raises(scdm.SessionError) as info:
        session.attach(host=bad_host)

    assert info.value.code == "invalid_host"
    assert fake.modeler_calls == []


def test_check_attach_args_normalises_without_side_effects():
    assert scdm.check_attach_args(" localhost ", "51790", " WNUA ") == (
        "localhost",
        51790,
        "wnua",
    )


# ---------------------------------------------------------------------------
# The guard: attach sessions cannot be closed
# ---------------------------------------------------------------------------


def test_close_on_an_attach_session_is_refused_before_the_library_is_reached(session, fake):
    session.attach()
    modeler = session.modeler

    with pytest.raises(scdm.SessionError) as info:
        session.close()

    assert info.value.code == "attach_mode_refuses_close"
    assert modeler.close_calls == 0, "the guard must fire before anything closes"
    assert session.connected is True, "the session is still usable after a refusal"


def test_close_refusal_carries_the_facts_not_just_the_refusal(session, fake):
    session.attach()

    with pytest.raises(scdm.SessionError) as info:
        session.close()

    extra = info.value.extra
    assert extra["process_untouched"] is True
    assert extra["licence_released"] is False
    assert extra["next_step"] == "scdm_detach"
    assert extra["session"]["mode"] == "attach"


def test_detach_on_an_attach_session_closes_only_the_channel(session, fake):
    session.attach()
    modeler = session.modeler

    result = session.detach()

    assert result["detached"] is True
    assert result["mode"] == "attach"
    assert result["process_untouched"] is True
    assert result["licence_released"] is False
    assert modeler.close_calls == 1
    assert session.connected is False


def test_detach_on_a_launch_session_is_refused(session, fake):
    """The mirror image: dropping the channel would strand a licensed process."""
    session.start()
    modeler = session.modeler

    with pytest.raises(scdm.SessionError) as info:
        session.detach()

    assert info.value.code == "detach_requires_attach"
    assert info.value.extra["next_step"] == "scdm_session_close"
    assert modeler.close_calls == 0
    assert session.connected is True


def test_detach_without_a_session_is_a_no_op():
    assert scdm.Session().detach() == {"detached": False, "reason": "no session"}


# -- teardown ---------------------------------------------------------------


def test_shutdown_detaches_an_attach_session_instead_of_raising(session, fake):
    session.attach()
    modeler = session.modeler

    result = session.shutdown()

    assert result["detached"] is True
    assert modeler.close_calls == 1


def test_shutdown_closes_a_launch_session(session, fake):
    session.start()

    assert session.shutdown()["closed"] is True


def test_shutdown_without_a_session_is_none():
    assert scdm.Session().shutdown() is None


# ---------------------------------------------------------------------------
# Server layer
# ---------------------------------------------------------------------------


def test_tools_are_plain_callables():
    """The rest of this section depends on it."""
    assert callable(server.scdm_session_start)
    assert callable(server.scdm_attach)
    assert inspect.isfunction(server.scdm_session_start)


def test_server_start_defaults_to_visible_and_says_so(live):
    session, _ = live
    result = server.scdm_session_start()

    assert result["ok"] is True
    assert result["mode"] == "launch"
    assert result["ui_visible"] is True
    assert result["session"]["mode"] == "launch"


def test_server_start_signature_default_is_false():
    default = inspect.signature(server.scdm_session_start).parameters["hidden"].default

    assert default is False


def test_server_start_hidden_true_reports_false(live):
    result = server.scdm_session_start(hidden=True)

    assert result["ui_visible"] is False


def test_server_start_on_an_attach_session_reports_attach_not_launch(live):
    session, _ = live
    session.attach()

    result = server.scdm_session_start(hidden=False)

    assert result["mode"] == "attach"
    assert result["ui_visible"] is None


def test_server_attach_returns_the_flunt_side_field_names(live):
    result = server.scdm_attach()

    assert result["ok"] is True
    assert result["mode"] == "attach"
    assert result["transport"]["host"] == "localhost"
    assert result["transport"]["port"] == scdm.DEFAULT_ATTACH_PORT
    assert result["transport"]["mode"] == "wnua"
    assert result["transport"]["encrypted"] is False
    assert result["licence"]["new_licence_consumed"] is False
    assert result["process_ownership"]["owner"] == "user"
    assert result["observed"]["backend_type"] == "BackendType.SPACECLAIM"


def test_server_attach_failure_is_structured_and_actionable(monkeypatch, live):
    monkeypatch.setattr(
        scdm, "_import_geometry", lambda: FakeAGC(error=RuntimeError("UNAVAILABLE"))
    )

    result = server.scdm_attach()

    assert result["ok"] is False
    assert result["error"] == "attach_unreachable"
    assert "set API_PORT=51790" in result["message"]


def test_server_close_in_attach_mode_is_refused_with_the_guard_facts(live):
    session, _ = live
    session.attach()

    result = server.scdm_session_close()

    assert result["ok"] is False
    assert result["error"] == "attach_mode_refuses_close"
    assert result["process_untouched"] is True
    assert result["licence_released"] is False
    assert result["next_step"] == "scdm_detach"
    assert session.modeler.close_calls == 0
    assert session.connected is True


def test_server_detach_in_attach_mode_succeeds(live):
    session, _ = live
    session.attach()

    result = server.scdm_detach()

    assert result["ok"] is True
    assert result["detached"] is True
    assert result["process_untouched"] is True
    assert result["licence_released"] is False
    assert session.connected is False


def test_server_detach_in_launch_mode_is_refused(live):
    session, _ = live
    server.scdm_session_start()

    result = server.scdm_detach()

    assert result["ok"] is False
    assert result["error"] == "detach_requires_attach"
    assert result["next_step"] == "scdm_session_close"
    assert session.connected is True


def test_server_close_in_launch_mode_still_works(live):
    session, _ = live
    server.scdm_session_start()

    result = server.scdm_session_close()

    assert result["ok"] is True
    assert result["licence_released"] is True
    assert session.connected is False


def test_server_status_carries_the_mode(live):
    session, _ = live

    assert server.scdm_session_status()["mode"] is None
    session.attach()
    assert server.scdm_session_status()["mode"] == "attach"


def test_a_blank_host_failure_is_reachable_through_the_server_tool(live):
    result = server.scdm_attach(host="  ")

    assert result["ok"] is False
    assert result["error"] == "invalid_host"


def test_existing_error_paths_still_report_the_generic_code(monkeypatch):
    """Pre-existing SessionError raises have no code; they must not break."""
    from ansys_bridge_mcp.scdm import SessionError

    assert server._err(SessionError("no SpaceClaim session"))["error"] == "session"


def test_attach_defaults_match_the_documented_conventions():
    parameters = inspect.signature(server.scdm_attach).parameters

    assert parameters["host"].default == "localhost"
    assert parameters["port"].default == 51790
    assert parameters["transport_mode"].default == "wnua"
    assert "wnua" in scdm.VALID_TRANSPORT_MODES


def test_instructions_document_both_modes():
    text = server.INSTRUCTIONS

    assert "mode=launch" in text
    assert "mode=attach" in text
    assert "CANNOT close the user's window" in text
    assert "double-click" in text
