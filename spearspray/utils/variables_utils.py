import re
import sys
import datetime as _dt
import logging
from typing import Any, Optional

from unidecode import unidecode

import spearspray.utils.constants as constants
from spearspray.utils.constants import GREEN, RED, YELLOW, RESET
from spearspray.modules.variables import VariablesManager

log = logging.getLogger(__name__)

def register_variables(variables: "VariablesManager") -> None:
    variables.register("name", "First name of the user", "")
    variables.register("samaccountname", "User logon name (sAMAccountName)", "")
    variables.register("year", "Year from pwdlastset (or whenCreated if applicable)", "")
    variables.register("short_year", "Last two digits of the year from pwdlastset (or whenCreated if applicable)", "")
    variables.register("month_number", "Month number from pwdlastset (or whenCreated if applicable)", "")
    variables.register("month_es", "Month name from pwdlastset (or whenCreated if applicable) in Spanish", "")
    variables.register("month_en", "Month name from pwdlastset (or whenCreated if applicable) in English", "")
    variables.register("season_es", "Season from pwdlastset (or whenCreated if applicable) in Spanish", "")
    variables.register("season_en", "Season from pwdlastset (or whenCreated if applicable) in English", "")
    variables.register("extra", "Extra word provided by user", "")
    variables.register("separator", "Separator character(s)", "")
    variables.register("suffix", "Suffix character(s)", "")

def print_registered_variables() -> None:
    variables_instance = VariablesManager()
    register_variables(variables_instance)
    variables_registered = variables_instance.get_all()

    print(f"{YELLOW}[*] Listing all available variables and their descriptions:{RESET}")
    
    for var in variables_registered:
        print(f"{GREEN}{{{var.name}}}{RESET}: {var.description}")

def are_all_variables_registered(patterns_detected: list, variables: list) -> bool:
    registered_variables = {var.name for var in variables}
    pattern_regex = re.compile(r'{(.*?)}')

    variables_not_registered = []
    for pattern in patterns_detected:
        
        pattern_raw = pattern["pattern"]
        found_variables = pattern_regex.findall(pattern_raw)

        for var in found_variables:
            if var not in registered_variables:
                variables_not_registered.append((var, pattern_raw))

    if variables_not_registered:
        for var, pattern_raw in variables_not_registered:
            log.error(f"[-] Variable '{var}' in pattern '{pattern_raw}' is not registered.")
        sys.exit(1)

    log.debug("[+] All variables are registered.")

def get_used_variables(variables: list, selected_pattern: str) -> list:
    used_variables = set(re.findall(r"\{(\w+)\}", selected_pattern))
    used_variables = [var for var in variables if var.name in used_variables]

    return used_variables

def _as_datetime(value: Any) -> Optional[_dt.datetime]:

    if isinstance(value, _dt.datetime):
        return value

    # The rest of the code assumes value is not a datetime object or value is None (weird case)

    if value is None or value == 0:
        return None

    if isinstance(value, list):
        value = _as_datetime(value[0]) if value else None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _dt.datetime.fromtimestamp(value, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            log.exception(f"{RED}[-]{RESET} Invalid timestamp value: {value}. Cannot convert to datetime.")
            return None

    if isinstance(value, str):
        try:
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            log.exception(f"{RED}[-]{RESET} Invalid ISO format string: {value}. Cannot convert to datetime.")
            return None

    return None

def variable_resolver(user: dict, selected_pattern: str, variables: list, extra: str, separator: str, suffix:str) -> str:

    values = {}

    pwd_dt  = _as_datetime(user.get("pwdLastSet"))
    when_dt = _as_datetime(user.get("whenCreated"))

    dt = pwd_dt or when_dt or _dt.datetime.now(_dt.timezone.utc)

    for var in variables:
        # Names
        if var.name == "name":
            raw_name = user.get("name")
            values[var.name] = unidecode(raw_name.split()[0]) if raw_name else user.get("sAMAccountName")
        elif var.name == "samaccountname":
            values[var.name] = user.get("sAMAccountName")
        
        # Years
        elif var.name == "year":
            values[var.name] = str(dt.year)
        elif var.name == "short_year":
            values[var.name] = str(dt.year)[-2:]
        
        # Months
        elif var.name == "month_number":
            values[var.name] = str(dt.month).zfill(2)
        elif var.name == "month_es":
            values[var.name] = constants.MONTH_NAMES_ES.get(dt.month, "")
        elif var.name == "month_en":
            values[var.name] = constants.MONTH_NAMES_EN.get(dt.month, "")
        
        # Seasons
        elif var.name == "season_es":
            values[var.name] = constants.SEASONS_ES.get(dt.month, "")
        elif var.name == "season_en":
            values[var.name] = constants.SEASONS_EN.get(dt.month, "")
        
        # Extra, separator and suffix
        elif var.name == "extra":
            values[var.name] = extra or ""
        elif var.name == "separator":
            values[var.name] = separator or ""
        elif var.name == "suffix":
            values[var.name] = suffix or ""

    return selected_pattern.format(**values)


_VOWEL_TO_NUMBER = str.maketrans("aeiouAEIOU", "4310743107")

def apply_leet(password: str) -> str:
    return password.translate(_VOWEL_TO_NUMBER)

