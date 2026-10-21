"""Exercise opt-in consent and atomic settings changes through the options flow."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cardata.const import (
    DOMAIN,
    OPTION_ENABLE_CHARGING_HISTORY,
    OPTION_ENABLE_EXTERNAL_POWER,
    OPTION_ENABLE_MAGIC_SOC,
    OPTION_ENABLE_TRIP_POLL,
    OPTION_ENABLE_TYRE_DIAGNOSIS,
    OPTION_NORMALIZE_SENSOR_STATES,
    OPTION_TRIP_POLL_COOLDOWN,
)
from custom_components.cardata.lifecycle import _make_options_listener
from custom_components.cardata.options_flow import CardataOptionsFlowHandler


async def _start_settings(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, options=options or {})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "action_settings"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "action_settings"
    return entry, result


def _settings(**overrides):
    return {
        OPTION_ENABLE_MAGIC_SOC: False,
        OPTION_ENABLE_CHARGING_HISTORY: False,
        OPTION_ENABLE_TYRE_DIAGNOSIS: False,
        OPTION_ENABLE_TRIP_POLL: True,
        OPTION_TRIP_POLL_COOLDOWN: 5,
        OPTION_ENABLE_EXTERNAL_POWER: False,
        OPTION_NORMALIZE_SENSOR_STATES: False,
        **overrides,
    }


@pytest.mark.asyncio
async def test_normalization_defaults_off(hass):
    """Existing entries without an option never opt in implicitly."""
    entry, result = await _start_settings(hass, {"unrelated": "preserved"})
    defaults = result["data_schema"]({})
    assert defaults[OPTION_NORMALIZE_SENSOR_STATES] is False
    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPTION_NORMALIZE_SENSOR_STATES] is False
    assert entry.options["unrelated"] == "preserved"


@pytest.mark.asyncio
async def test_enable_requires_explicit_confirmation_before_any_changes(hass):
    """Reject unchecked consent and defer entity cleanup and all options."""
    original = {OPTION_ENABLE_MAGIC_SOC: True, "unrelated": "preserved"}
    entry, result = await _start_settings(hass, original)
    registry = er.async_get(hass)
    magic = registry.async_get_or_create("sensor", DOMAIN, "VIN_vehicle.magic_soc", config_entry=entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings(normalize_sensor_states=True)
    )
    assert result["step_id"] == "confirm_state_normalization"
    assert result["data_schema"]({}) == {"confirm": False}
    assert dict(entry.options) == original
    assert registry.async_get(magic.entity_id) is not None

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"confirm": "confirm"}
    assert dict(entry.options) == original
    assert registry.async_get(magic.entity_id) is not None

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": True})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPTION_NORMALIZE_SENSOR_STATES] is True
    assert entry.options["unrelated"] == "preserved"
    assert entry.options[OPTION_ENABLE_MAGIC_SOC] is False
    assert registry.async_get(magic.entity_id) is None


@pytest.mark.asyncio
async def test_cancel_confirmation_preserves_options_and_registry(hass):
    """Abandoning the flow discards pending settings and their side effects."""
    original = {OPTION_ENABLE_MAGIC_SOC: True, "unrelated": 42}
    entry, result = await _start_settings(hass, original)
    registry = er.async_get(hass)
    magic = registry.async_get_or_create("sensor", DOMAIN, "VIN_vehicle.magic_soc", config_entry=entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings(normalize_sensor_states=True)
    )
    hass.config_entries.options.async_abort(result["flow_id"])
    assert dict(entry.options) == original
    assert registry.async_get(magic.entity_id) is not None
    fresh = await hass.config_entries.options.async_init(entry.entry_id)
    fresh = await hass.config_entries.options.async_configure(fresh["flow_id"], {"next_step_id": "action_settings"})
    assert fresh["data_schema"]({})[OPTION_NORMALIZE_SENSOR_STATES] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,current", [(False, False), (True, True), (True, False)])
async def test_no_confirmation_without_enabling_transition(hass, previous, current):
    """Keeping either mode or switching back does not require consent again."""
    entry, result = await _start_settings(hass, {OPTION_NORMALIZE_SENSOR_STATES: previous, "other": "value"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings(normalize_sensor_states=current)
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPTION_NORMALIZE_SENSOR_STATES] is current
    assert entry.options["other"] == "value"


@pytest.mark.asyncio
async def test_confirmed_setting_reloads_through_existing_listener(hass):
    """Only persisted changes trigger the integration's existing reload hook."""
    entry, result = await _start_settings(hass)
    listener = _make_options_listener(dict(entry.options))
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _settings(normalize_sensor_states=True)
        )
        await listener(hass, entry)
        reload.assert_not_awaited()
        await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": True})
        await listener(hass, entry)
        reload.assert_awaited_once_with(entry.entry_id)
        await listener(hass, entry)
        reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_returning_to_settings_discards_pending_consent(hass):
    """Old confirmation cannot apply settings after returning to edit them."""
    entry = MockConfigEntry(domain=DOMAIN, options={"unrelated": "preserved"})
    entry.add_to_hass(hass)
    flow = CardataOptionsFlowHandler(entry)
    flow.hass = hass
    result = await flow.async_step_action_settings(_settings(normalize_sensor_states=True))
    assert result["step_id"] == "confirm_state_normalization"
    result = await flow.async_step_action_settings()
    assert result["step_id"] == "action_settings"
    result = await flow.async_step_confirm_state_normalization({"confirm": True})
    assert result["step_id"] == "action_settings"
    assert dict(entry.options) == {"unrelated": "preserved"}
