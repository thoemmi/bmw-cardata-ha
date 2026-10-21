"""Entity translations and compatibility with existing registry entries."""

import csv
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.template import Template
from homeassistant.helpers.translation import async_get_translations, async_translate_state
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cardata.binary_sensor import OPENING_STATUS_TITLES, CardataBinarySensor
from custom_components.cardata.const import DOMAIN
from custom_components.cardata.descriptor_state import DescriptorState
from custom_components.cardata.descriptor_titles import DESCRIPTOR_TITLES
from custom_components.cardata.entity import SENSOR_TITLES, CardataEntity
from custom_components.cardata.sensor import CardataSensor
from custom_components.cardata.sensor_charging_energy import CardataChargingEnergySensor
from custom_components.cardata.sensor_diagnostics import (
    CardataChargingHistorySensor,
    CardataDiagnosticsSensor,
    CardataEfficiencyLearningSensor,
    CardataTyreDiagnosisSensor,
    CardataVehicleMetadataSensor,
)
from custom_components.cardata.state_translations import NORMALIZED_SENSOR_STATES, normalize_sensor_state

VIN = "WBA00000000000001"
SOC = "vehicle.powertrain.electric.battery.stateOfCharge.displayed"
DOOR = "vehicle.cabin.door.row1.driver.isOpen"
WINDOW = "vehicle.cabin.window.row1.driver.status"
TRANSLATIONS = Path(__file__).resolve().parents[1] / "custom_components/cardata/translations"


@pytest.fixture
def coordinator():
    """Provide data without contacting BMW or starting background workers."""
    return SimpleNamespace(
        entry_id="test-entry",
        names={VIN: "i4"},
        device_metadata={},
        signal_update="cardata_test_update",
        signal_metadata="cardata_test_metadata",
        signal_efficiency_learning="cardata_test_efficiency",
        signal_charging_history="cardata_test_history",
        signal_tyre_diagnosis="cardata_test_tyres",
        signal_diagnostics="cardata_test_diagnostics",
        get_state=Mock(return_value=None),
        get_charging_history=Mock(return_value=[]),
        has_charging_history=Mock(return_value=False),
        get_tyre_diagnosis=Mock(return_value={}),
        get_efficiency_learning_attributes=Mock(return_value={}),
        _soc_predictor=SimpleNamespace(get_learned_efficiency=Mock(return_value=None)),
        connection_status="connected",
        last_message_at=None,
        last_telematic_api_at=None,
        last_disconnect_reason=None,
    )


@pytest.fixture
async def platforms(hass, entity_registry, device_registry):
    """Use HA's real platform, translation loader and entity/device registries."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    # Translation helpers look up resources for loaded integrations.
    hass.config.components.add(DOMAIN)
    active = []

    async def add(entities, domain="sensor", language="de"):
        hass.config.language = language
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain=domain,
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        platform.config_entry = entry
        platform.async_prepare()
        active.append(platform)
        await platform.platform_data.async_load_translations()
        await platform.async_add_entities(entities)
        await hass.async_block_till_done()
        for entity in entities:
            assert entity.entity_id in platform.entities
        return platform

    yield entry, add
    for platform in reversed(active):
        await platform.async_destroy()
    await hass.async_block_till_done()


@pytest.mark.parametrize("language", ["de", "en", "nl"])
@pytest.mark.parametrize(
    "descriptor,german_name",
    [
        (SOC, "Hochvoltbatterie – Ladezustand"),
        ("vehicle.status.conditionBasedServices.engineOil", "Service – Motoröl"),
        ("vehicle.electronicControlUnit.diagnosticTroubleCodes.raw", "Fahrzeug – Fehlerspeicher"),
    ],
)
async def test_uses_native_translations_and_english_fallback(
    hass, platforms, coordinator, language, descriptor, german_name
):
    """Languages without entity translations fall back to en.json."""
    _, add = platforms
    sensor = CardataSensor(coordinator, VIN, descriptor)
    await add([sensor], language=language)
    expected = german_name if language == "de" else DESCRIPTOR_TITLES[descriptor]
    assert sensor.name == expected
    assert hass.states.get(sensor.entity_id).attributes["friendly_name"] == f"i4 {expected}"


@pytest.mark.parametrize(
    ("factory", "domain", "descriptor", "suffix"),
    [
        (CardataSensor, "sensor", SOC, ""),
        (CardataBinarySensor, "binary_sensor", DOOR, ""),
        (CardataBinarySensor, "binary_sensor", WINDOW, "_open"),
    ],
)
@pytest.mark.parametrize("custom_name", [None, "Mein eigener Name"])
async def test_registry_ids_and_custom_names_survive_reload(
    hass, platforms, coordinator, factory, domain, descriptor, suffix, custom_name
):
    """Upgrade an old registry row, then reload it in a second language."""
    entry, add = platforms
    registry = er.async_get(hass)
    unique_id = f"{VIN}_{descriptor}{suffix}"
    old = registry.async_get_or_create(
        domain,
        DOMAIN,
        unique_id,
        config_entry=entry,
        suggested_object_id="my_existing_entity_id",
        original_name="i4 Old English Name",
        has_entity_name=False,
    )
    registry.async_update_entity(old.entity_id, name=custom_name)
    original_id = old.id

    for language in ("de", "en"):
        sensor = factory(coordinator, VIN, descriptor)
        platform = await add([sensor], domain, language)
        current = registry.async_get(old.entity_id)
        assert current.id == original_id
        assert current.unique_id == unique_id
        assert current.name == custom_name
        assert sensor.entity_id == old.entity_id
        assert current.original_name == sensor.name
        assert current.has_entity_name is True
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 1
        friendly = hass.states.get(old.entity_id).attributes["friendly_name"]
        assert friendly == (custom_name if custom_name is not None else f"i4 {sensor.name}")
        await platform.async_reset()
        await hass.async_block_till_done()


async def test_opening_binary_name_differs_from_string_sensor(hass, platforms, coordinator):
    """The derived open/closed entity keeps its suffix and its distinct name."""
    _, add = platforms
    string_sensor = CardataSensor(coordinator, VIN, WINDOW)
    binary_sensor = CardataBinarySensor(coordinator, VIN, WINDOW)
    await add([string_sensor])
    await add([binary_sensor], "binary_sensor")
    assert string_sensor.name != binary_sensor.name
    assert "geöffnet" in binary_sensor.name.lower()
    assert binary_sensor.unique_id == f"{VIN}_{WINDOW}_open"
    assert string_sensor.unique_id == f"{VIN}_{WINDOW}"


async def test_device_rename_and_legacy_signal_do_not_overwrite_translation(hass, platforms, coordinator):
    """HA owns the device prefix; the old integration signal cannot reset it."""
    _, add = platforms
    sensor = CardataSensor(coordinator, VIN, SOC)
    await add([sensor])
    devices = dr.async_get(hass)
    devices.async_update_device(sensor.device_entry.id, name_by_user="Mein BMW")
    async_dispatcher_send(hass, f"{DOMAIN}_{coordinator.entry_id}_name", VIN, "New Vehicle Name")
    await hass.async_block_till_done()
    assert sensor.name == "Hochvoltbatterie – Ladezustand"
    assert hass.states.get(sensor.entity_id).attributes["friendly_name"] == "Mein BMW Hochvoltbatterie – Ladezustand"


@pytest.mark.parametrize("factory,domain", [(CardataSensor, "sensor"), (CardataBinarySensor, "binary_sensor")])
async def test_unknown_descriptors_keep_readable_fallback(platforms, coordinator, factory, domain):
    """A new BMW descriptor works even before it has a translation."""
    _, add = platforms
    sensor = factory(coordinator, VIN, "vehicle.future.new_value")
    await add([sensor], domain)
    assert sensor.translation_key is None
    assert sensor.name == "Future New Value"
    assert sensor.unique_id == f"{VIN}_vehicle.future.new_value"


@pytest.mark.parametrize(
    "factory",
    [
        CardataVehicleMetadataSensor,
        CardataEfficiencyLearningSensor,
        CardataChargingHistorySensor,
        CardataChargingEnergySensor,
        CardataTyreDiagnosisSensor,
    ],
)
async def test_vehicle_helper_sensors_are_translated(hass, platforms, coordinator, factory):
    """Helpers without catalogue descriptors use the same translation machinery."""
    entry, add = platforms
    sensor = factory(coordinator, VIN)
    # The efficiency helper is disabled by default, so represent an enabled
    # existing installation without changing that default in production.
    er.async_get(hass).async_get_or_create("sensor", DOMAIN, sensor.unique_id, config_entry=entry)
    await add([sensor])
    de = json.loads((TRANSLATIONS / "de.json").read_text())["entity"]["sensor"]
    assert sensor.name == de[sensor.translation_key]["name"]
    assert sensor.unique_id == f"{VIN}_{sensor.descriptor}"


@pytest.mark.parametrize("sensor_type", ["last_message", "last_telematic_api", "connection_status"])
async def test_connection_diagnostics_are_translated(platforms, coordinator, sensor_type):
    entry, add = platforms
    sensor = CardataDiagnosticsSensor(coordinator, SimpleNamespace(debug_info={}), entry.entry_id, sensor_type)
    await add([sensor])
    de = json.loads((TRANSLATIONS / "de.json").read_text())["entity"]["sensor"]
    assert sensor.name == de[sensor_type]["name"]
    assert sensor.unique_id == f"{entry.entry_id}_diagnostics_{sensor_type}"


def test_base_entity_for_other_platforms_keeps_legacy_naming(coordinator):
    """The device tracker keeps the existing naming behaviour."""
    entity = CardataEntity(coordinator, VIN, SOC)
    assert entity.name == f"i4 {DESCRIPTOR_TITLES[SOC]}"
    assert entity.has_entity_name is False


def test_catalogue_coverage_and_valid_translation_keys():
    """Prevent missing translations and collisions as descriptors are added."""
    en = json.loads((TRANSLATIONS / "en.json").read_text())["entity"]
    de = json.loads((TRANSLATIONS / "de.json").read_text())["entity"]
    descriptor_keys = {descriptor.lower().replace(".", "_") for descriptor in DESCRIPTOR_TITLES}
    assert len(descriptor_keys) == len(DESCRIPTOR_TITLES)
    for domain in ("sensor", "binary_sensor"):
        assert descriptor_keys <= en[domain].keys()
        assert en[domain].keys() == de[domain].keys()
        for key in en[domain]:
            assert re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key)
            assert en[domain][key]["name"].strip()
            assert de[domain][key]["name"].strip()
        for descriptor, name in DESCRIPTOR_TITLES.items():
            key = descriptor.lower().replace(".", "_")
            expected = OPENING_STATUS_TITLES.get(descriptor, name) if domain == "binary_sensor" else name
            assert en[domain][key]["name"] == expected
    assert SENSOR_TITLES.keys() <= en["sensor"].keys()


async def test_late_vehicle_name_updates_prefix_without_changing_id(hass, platforms, coordinator):
    """Metadata may only arrive after the entity was registered with a VIN."""
    _, add = platforms
    coordinator.names.clear()
    sensor = CardataSensor(coordinator, VIN, SOC)
    await add([sensor])
    entity_id = sensor.entity_id
    original_name = sensor.name
    assert hass.states.get(entity_id).attributes["friendly_name"] == f"{VIN} {original_name}"
    coordinator.names[VIN] = "i4 M60"
    async_dispatcher_send(hass, f"{DOMAIN}_{coordinator.entry_id}_name", VIN, "i4 M60")
    await hass.async_block_till_done()
    assert sensor.name == original_name
    assert sensor.entity_id == entity_id
    assert hass.states.get(entity_id).attributes["friendly_name"] == f"i4 M60 {original_name}"


@pytest.mark.parametrize("language", ["de", "en", "nl"])
@pytest.mark.parametrize(
    "descriptor,raw_value,german,english",
    [
        (
            "vehicle.powertrain.electric.battery.charging.preferenceSmartCharging",
            "PRICE_OPTIMIZED",
            "Preisoptimiert",
            "Price-optimized",
        ),
        (
            "vehicle.powertrain.electric.battery.charging.preferenceSmartCharging",
            "RENEWABLE_ENERGEY",
            "Erneuerbare Energien bevorzugen",
            "Renewable energy",
        ),
        ("vehicle.drivetrain.electricEngine.charging.status", "NOCHARGING", "Lädt nicht", "Not charging"),
        ("vehicle.cabin.window.row1.driver.status", "CLOSED", "Geschlossen", "Closed"),
        ("vehicle.vehicle.preConditioning.error", "LowBattery", "Batterieladung niedrig", "Low battery"),
        (
            "vehicle.vehicle.antiTheftAlarmSystem.alarm.armStatus",
            "doorsTiltCabin",
            "Türen, Neigung und Innenraum",
            "Doors, tilt and cabin",
        ),
        ("vehicle.drivetrain.electricEngine.charging.phaseNumber", "3-PHASES", "Dreiphasig", "Three-phase"),
        ("vehicle.cabin.door.lock.status", "SELECTIVE-LOCKED", "Selektiv verriegelt", "Selectively locked"),
        (
            "vehicle.powertrain.electric.departureTime.displayControl",
            "NO_DISPLAY_DEPARTURE_TIME",
            "Abfahrtszeit nicht anzeigen",
            "Do not display departure time",
        ),
        (
            "vehicle.drivetrain.electricEngine.charging.profile.mode",
            "IMMEDIATE_CHARGING",
            "Sofortladen",
            "Immediate charging",
        ),
        ("vehicle.cabin.door.status", "SECURED", "Gesichert", "Secured"),
        ("vehicle.vehicle.preConditioning.activity", "INACTIVE", "Inaktiv", "Inactive"),
        ("vehicle.vehicle.preConditioning.activity", "inactive", "Inaktiv", "Inactive"),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
async def test_state_display_and_automation_values_follow_opt_in(
    hass, platforms, coordinator, language, descriptor, raw_value, german, english, enabled
):
    """Legacy mode preserves raw states; only explicit opt-in changes them."""
    entry, add = platforms
    coordinator.normalize_sensor_states = enabled
    coordinator.get_state.return_value = DescriptorState(value=raw_value, unit=None, timestamp=None)
    sensor = CardataSensor(coordinator, VIN, descriptor)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, sensor.unique_id, config_entry=entry, suggested_object_id="existing_bmw_sensor"
    )
    registry.async_update_entity(old.entity_id, name="Mein Sensor")
    await add([sensor], language=language)
    assert sensor.entity_id == old.entity_id
    assert registry.async_get(old.entity_id).name == "Mein Sensor"
    published = normalize_sensor_state(descriptor, raw_value) if enabled else raw_value
    assert sensor.native_value == published
    state = hass.states.get(old.entity_id)
    assert state.state == published
    if enabled:
        assert state.attributes["raw_value"] == raw_value
    else:
        assert "raw_value" not in state.attributes
    assert coordinator.get_state.return_value.value == raw_value
    assert Template("{{ states('" + old.entity_id + "') }}", hass).async_render() == published
    assert Template("{{ is_state('" + old.entity_id + "', '" + published + "') }}", hass).async_render() is True
    expected = (german if language == "de" else english) if enabled else raw_value
    assert Template("{{ state_translated('" + old.entity_id + "') }}", hass).async_render() == expected
    # Translation must not impose a closed enum that rejects future BMW values.
    assert sensor.options is None
    coordinator.get_state.return_value = DescriptorState(value="NEW_BMW_MODE", unit=None, timestamp=None)
    sensor._handle_update(VIN, descriptor)
    await hass.async_block_till_done()
    assert hass.states.get(old.entity_id).state == "NEW_BMW_MODE"
    assert Template("{{ state_translated('" + old.entity_id + "') }}", hass).async_render() == "NEW_BMW_MODE"


@pytest.mark.parametrize("language", ["de", "en"])
async def test_all_state_labels_load_with_normalized_keys(hass, language):
    """Exercise HA's real translation loader for every opt-in state label."""
    hass.config.language = language
    hass.config.components.add(DOMAIN)
    await async_get_translations(hass, language, "entity", {DOMAIN})
    data = json.loads((TRANSLATIONS / f"{language}.json").read_text())["entity"]["sensor"]
    for key, entity in data.items():
        for raw_value, label in entity.get("state", {}).items():
            # HA handles its reserved unavailable/unknown states globally.
            if raw_value in ("unavailable", "unknown"):
                continue
            assert async_translate_state(hass, raw_value, "sensor", DOMAIN, key, None) == label
    key = "vehicle_powertrain_electric_battery_charging_preferencesmartcharging"
    assert async_translate_state(hass, "price_optimized", "sensor", DOMAIN, key, None) == "price_optimized"
    assert (
        async_translate_state(hass, "PRICE_OPTIMIZED", "sensor", DOMAIN, key + "_normalized", None) == "PRICE_OPTIMIZED"
    )


def test_fixed_catalogue_state_coverage_and_language_parity():
    """Cover finite catalogue values, excluding numeric ranges and normalized motion booleans."""
    en = json.loads((TRANSLATIONS / "en.json").read_text())["entity"]["sensor"]
    de = json.loads((TRANSLATIONS / "de.json").read_text())["entity"]["sensor"]
    with (TRANSLATIONS.parents[2] / "tools/Full list.csv").open(encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            descriptor = row["Technical descriptor"].replace("\u200b", "")
            if row["Data type"] not in ("string", "-", "boolean") or descriptor == "vehicle.isMoving":
                continue
            raw_values = [v.strip() for v in re.split(r"[,;]", row["Typical value range"])]
            if len(raw_values) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in raw_values):
                continue
            key = descriptor.lower().replace(".", "_")
            assert set(raw_values) <= NORMALIZED_SENSOR_STATES[descriptor].keys(), descriptor
            assert {NORMALIZED_SENSOR_STATES[descriptor][v] for v in raw_values} <= en[key + "_normalized"][
                "state"
            ].keys()
    for key, entity in en.items():
        labels = entity.get("state", {})
        assert labels.keys() == de[key].get("state", {}).keys(), key
        for raw_value, label in labels.items():
            assert label and label == label.strip()
            assert de[key]["state"][raw_value] and de[key]["state"][raw_value] == de[key]["state"][raw_value].strip()


@pytest.mark.parametrize("enabled", [False, True])
async def test_lowercase_states_are_only_translated_when_enabled(hass, platforms, coordinator, enabled):
    """Even unchanged lowercase codes must keep their original display by default."""
    _, add = platforms
    coordinator.normalize_sensor_states = enabled
    descriptor = "vehicle.sim.status"
    coordinator.get_state.return_value = DescriptorState(value="true", unit=None, timestamp=None)
    sensor = CardataSensor(coordinator, VIN, descriptor)
    await add([sensor])
    assert sensor.native_value == "true"
    de = json.loads((TRANSLATIONS / "de.json").read_text())["entity"]["sensor"]
    expected = de[sensor.translation_key]["state"]["true"] if enabled else "true"
    assert async_translate_state(hass, "true", "sensor", DOMAIN, sensor.translation_key, None) == expected


def test_state_mapping_keys_are_valid_unique_and_complete():
    """Keep mappings stable, exhaustive, unambiguous and distinct from HA sentinels."""
    en = json.loads((TRANSLATIONS / "en.json").read_text())["entity"]["sensor"]
    for descriptor, mapping in NORMALIZED_SENSOR_STATES.items():
        key = descriptor.lower().replace(".", "_")
        assert "state" not in en[key]
        normalized = en[key + "_normalized"]
        assert normalized["name"] == en[key]["name"]
        assert set(mapping.values()) == set(normalized["state"])
        # This observed BMW case variant intentionally shares the same meaning.
        aliases = {"INACTIVE"} if descriptor == "vehicle.vehicle.preConditioning.activity" else set()
        assert len(mapping) - len(aliases) == len(set(mapping.values()))
        for raw, value in mapping.items():
            assert re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value)
            assert value not in ("unknown", "unavailable")
            assert normalize_sensor_state(descriptor, raw) == value
        for future in ("FUTURE_BMW_CODE", "Future BMW code", "", None, 12.5, True):
            assert normalize_sensor_state(descriptor, future) == future
    assert normalize_sensor_state("vehicle.future.status", "UNKNOWN") == "UNKNOWN"


@pytest.mark.parametrize(
    "descriptor,raw_value,normalized_value",
    [
        ("vehicle.powertrain.electric.battery.charging.preferenceSmartCharging", "PRICE_OPTIMIZED", "price_optimized"),
        (
            "vehicle.powertrain.electric.departureTime.displayControl",
            "NO_DISPLAY_DEPARTURE_TIME",
            "no_display_departure_time",
        ),
        ("vehicle.drivetrain.electricEngine.charging.profile.mode", "IMMEDIATE_CHARGING", "immediate_charging"),
        ("vehicle.cabin.door.status", "SECURED", "secured"),
        ("vehicle.vehicle.preConditioning.activity", "INACTIVE", "inactive"),
        ("vehicle.vehicle.preConditioning.activity", "inactive", "inactive"),
    ],
)
async def test_mode_switch_preserves_registry_and_restores_raw_value(
    hass, platforms, coordinator, descriptor, raw_value, normalized_value
):
    """Opt in/out with an HA restore snapshot, without waiting for a BMW update."""
    entry, add = platforms
    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "sensor", DOMAIN, f"{VIN}_{descriptor}", config_entry=entry, suggested_object_id="existing_smart_charging"
    )
    registry.async_update_entity(row.entity_id, name="Mein Ladetarif")
    # Simulate a reload with no live coordinator data; use the previous HA state.
    last_state = None
    coordinator.async_restore_descriptor_state = AsyncMock()
    for enabled in (False, True, False):
        coordinator.normalize_sensor_states = enabled
        coordinator.get_state.return_value = None
        sensor = CardataSensor(coordinator, VIN, descriptor)
        if last_state is None:
            coordinator.get_state.return_value = DescriptorState(value=raw_value, unit=None, timestamp=None)
        with patch.object(sensor, "async_get_last_state", new=AsyncMock(return_value=last_state)):
            platform = await add([sensor])
        current = hass.states.get(row.entity_id)
        assert current.state == (normalized_value if enabled else raw_value)
        assert sensor._attr_native_value == raw_value
        assert registry.async_get(row.entity_id).id == row.id
        assert registry.async_get(row.entity_id).name == "Mein Ladetarif"
        assert sensor.entity_id == row.entity_id
        if last_state is not None:
            assert coordinator.async_restore_descriptor_state.call_args.args[2] == raw_value
        last_state = current
        await platform.async_reset()
        await hass.async_block_till_done()


@pytest.mark.parametrize("enabled", [False, True])
async def test_normalization_leaves_numeric_sensors_and_binary_sensors_unchanged(hass, platforms, coordinator, enabled):
    """The option changes fixed text states only, without touching shared BMW data."""
    _, add = platforms
    coordinator.normalize_sensor_states = enabled
    numeric = DescriptorState(value=86, unit="%", timestamp=None)
    door = DescriptorState(value="OPEN", unit=None, timestamp=None)
    coordinator.get_state.side_effect = lambda vin, descriptor: numeric if descriptor == SOC else door
    sensor = CardataSensor(coordinator, VIN, SOC)
    binary = CardataBinarySensor(coordinator, VIN, WINDOW)
    await add([sensor])
    await add([binary], "binary_sensor")
    assert hass.states.get(sensor.entity_id).state == "86"
    assert hass.states.get(sensor.entity_id).attributes["unit_of_measurement"] == "%"
    assert "raw_value" not in sensor.extra_state_attributes
    assert hass.states.get(binary.entity_id).state == "on"
    assert "raw_value" not in binary.extra_state_attributes
    assert door.value == "OPEN"


@pytest.mark.parametrize("enabled", [False, True])
async def test_helper_state_translations_are_opt_in(hass, platforms, coordinator, enabled):
    """Already-lowercase diagnostic states do not gain translated displays by default."""
    entry, add = platforms
    coordinator.normalize_sensor_states = enabled
    coordinator.device_metadata[VIN] = {"name": "i4"}
    metadata = CardataVehicleMetadataSensor(coordinator, VIN)
    connection = CardataDiagnosticsSensor(
        coordinator, SimpleNamespace(debug_info={}), entry.entry_id, "connection_status"
    )
    await add([metadata, connection])
    for sensor, raw in ((metadata, "available"), (connection, "connected")):
        state = hass.states.get(sensor.entity_id)
        assert state.state == raw
        label = async_translate_state(hass, raw, "sensor", DOMAIN, sensor.translation_key, None)
        assert (label != raw) is enabled
        if enabled:
            assert state.attributes["raw_value"] == raw
        else:
            assert "raw_value" not in state.attributes
