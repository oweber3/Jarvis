# Current time tool

`getTime` returns deterministic current time/date data without model processing.
An optional `location` names a city/country or a valid IANA timezone. A valid
timezone uses local timezone arithmetic; named places use the existing
Open-Meteo geocoding lookup with the tool-execution timeout and cached results.
Unknown places, unavailable timezones and network failures return honest errors.

With no location, the tool uses the user's GeoIP timezone where configured and
available, otherwise the OS timezone. `local_only: true` selects the OS clock
and timezone without GeoIP, public-IP discovery or geocoding, even when a
location argument is also present. The deterministic
fast router supplies this flag for local time/date queries. Location-aware
conversational requests retain the optional location path.

Results use `format_time_context`, including date, weekday, time and timezone.
The existing central tool executor and safety policy govern every invocation.
