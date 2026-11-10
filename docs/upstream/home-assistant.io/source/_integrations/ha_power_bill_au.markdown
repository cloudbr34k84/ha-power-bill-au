---
title: HA Power Bill AU
description: Calculate electricity costs and feed-in credits from your Home Assistant energy meters and tariff plans.
ha_category:
  - Energy
  - Sensor
ha_iot_class: Calculated
ha_config_flow: true
ha_codeowners:
  - '@cloudbr34k84'
ha_domain: ha_power_bill_au
ha_platforms:
  - diagnostics
  - sensor
ha_integration_type: service
---

The **HA Power Bill AU** {% term integration %} calculates electricity costs from energy
sensors already in Home Assistant. Enter your electricity contract's rates and
charges to track today's costs, monthly costs, lifetime costs and solar feed-in
credits. Dated tariff plans let you apply rate changes to the appropriate usage.

HA Power Bill AU uses local sensor readings and [Recorder](/integrations/recorder/)
statistics. It does not connect to your energy retailer or require a retailer account.
Costs use Australian dollars and Australian GST rules.

## Prerequisites

1. Enable [Recorder](/integrations/recorder/) and include the energy sensors you want to use.
2. Set up an import energy sensor with energy device class, state class `total` or
   `total_increasing`, and a unit of Wh, kWh or MWh. The sensor must currently report
   a finite, nonnegative reading. Add an export sensor if you want feed-in credits.
3. Find your tariff's import rates, feed-in rates, daily charges and GST treatment
   on your electricity bill. Rates must cover every hour of the day exactly once.

{% include integrations/config_flow.md %}

Choose the import meter, optionally choose a distinct export meter, enter the
import and export bands, and then enter the daily charges. Each schedule supports
up to three bands. Hours are whole numbers: 0 is midnight and 24 is the end of the
day. A band can cross midnight, such as 21 to 11. Leave a band's name blank to
leave that slot unused.

{% configuration_basic %}
Energy provider name:
  description: >
    The contract label used for its device and sensors. Enter your retailer's name
    or another meaningful label.
Import energy sensor (Wh, kWh or MWh):
  description: >
    The cumulative energy sensor measuring electricity imported from the grid.
Export energy sensor (optional):
  description: >
    An optional cumulative energy sensor measuring electricity exported to the grid.
Band name:
  description: >
    A label such as peak, shoulder, off-peak or feed-in. A blank name leaves that
    slot unused.
Start hour:
  description: >
    The inclusive whole-hour start of the band, from 0 to 23.
End hour:
  description: >
    The exclusive whole-hour end of the band, from 1 to 24. An end earlier than the
    start crosses midnight.
Rate:
  description: >
    The import price or feed-in credit in AUD per kWh for that band.
Rates include GST:
  description: >
    Whether the import rates and daily charges you enter already include GST.
    Feed-in credits are not increased by GST.
Daily supply charge (AUD/day):
  description: >
    The fixed supply charge in AUD per day.
Daily membership fee (AUD/day):
  description: >
    An additional fixed membership charge in AUD per day, or zero if there is none.
Track cost of these energy sensors (optional):
  description: >
    Optional additional cumulative energy sensors, such as an EV charger. These are
    priced with the import tariff and must be distinct from the import and export
    meters.
{% endconfiguration_basic %}

{% include integrations/option_flow.md %}

{% configuration_basic %}
Energy provider name:
  description: >
    Change the contract label.
Add a plan from a date:
  description: >
    Enter the date on which new rates or daily charges begin. Existing plans
    continue to price usage before that date. Saving a plan reloads the integration
    and reprices history.
Demand charge:
  description: >
    Choose the applicable plan and enter its AUD per kW per day rate. Enter billed
    monthly demand as one month per line, for example 2026-11: 13.2. To estimate
    demand instead, enable estimation and set the daily demand window and a 5, 15,
    30 or 60 minute averaging block to match your contract. A billed monthly value
    overrides the estimate.
Stepped feed-in:
  description: >
    For a plan with one export band, enter the first rate, its daily kWh cap and the
    rate after the cap. Time-of-use feed-in plans with multiple bands do not support
    this option.
Tracked devices:
  description: >
    Add or remove extra energy sensors whose electricity costs you want to track.
    Removing a tracked sensor removes its cost entities.
{% endconfiguration_basic %}

## Supported functionality

### Sensors

- **Current tariff**: The active import band, with the current GST-inclusive rate and next change as attributes.
- **Current import rate**: The active GST-inclusive import price in AUD per kWh.
- **Import cost today** and **Import cost month to date**: Usage costs including fixed and applicable demand charges.
- **Net cost today** and **Net cost month to date**: Import costs less export credits.
- **Export credit today** and **Export credit month to date**: Feed-in credits when an export meter is configured.
- **Import cost total (Energy dashboard)** and **Export credit total (Energy dashboard)**: Lifetime cost and credit totals suitable for the Energy dashboard's cost fields.
  To select them, open the integration's **Configure** menu and choose **Add to the Energy dashboard**. It asks for confirmation and only fills empty cost fields of matching grid meters.
- **Band energy today** and **Band energy month to date**: Energy for each configured import or export band. Stepped feed-in reports a separate value for each step.
- **Peak demand this month**: The demand being charged, with estimated and billed values in attributes when a demand charge is configured.
- **Tracked-device costs**: Today, month-to-date and lifetime costs for each additional meter.

## Data updates

Costs update when the source meters change. A local one-minute timer handles
tariff transitions and day changes. Optional demand estimation reads Recorder's
five-minute statistics every five minutes. History is rebuilt from Recorder on
each integration reload.

## Known limitations

- Each contract uses one import meter and, optionally, one export meter.
- Time-of-use bands use whole hours and must cover the full day without overlaps.
- Costs currently use AUD and Australian GST rules.
- Demand estimates can differ from the retailer's billed demand. Enter billed kW
  values to reconcile a month's demand charge. Estimation cannot recover peaks
  from short-term statistics that Recorder has already purged.
- Hourly history takes priority over daily history. Daily-only history is priced
  using the plan's noon rate and cannot reproduce time-of-use detail.
- Fixed charges start at the first available import-meter history date, or the
  initial setup date when no history exists. History before the first dated plan
  uses that plan's rates.
- Residential spike filters exclude more than 30 kWh in an hourly statistics row,
  300 kWh in a daily row or 4 kWh in a five-minute demand slot. High-capacity usage
  may exceed these limits.

## Troubleshooting

{% details "Costs are unavailable" %}

Confirm that the source meter has energy device class, state class `total` or
`total_increasing`, a unit of Wh, kWh or MWh, and a finite, nonnegative reading.
Tariff information remains available while a source meter is unavailable. Costs
resume updating after valid readings return.

{% enddetails %}

{% details "Some hours of the day are not covered" %}

Check that your named bands cover all 24 hours exactly once. Blank band names
disable those slots. Use 24 for a band ending at midnight and make each band's
end match the next band's start.

{% enddetails %}

## Removing the integration

This integration follows standard integration removal.

{% include integrations/remove_device_service.md %}

Removing a contract deletes its locally stored usage ledger and saved demand
peaks. The source energy sensors and their Recorder statistics remain. Recreating
the contract can rebuild available history, but cannot recover saved demand peaks
whose source statistics have already been purged. There is no retailer account,
token or external device configuration to clean up.
