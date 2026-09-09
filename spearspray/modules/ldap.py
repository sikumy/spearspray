import logging
import ssl
from datetime import timedelta
from typing import Dict, List, Iterable, Optional

import ldap3
from ldap3 import NTLM, SIMPLE
from ldap3.core.exceptions import LDAPException, LDAPBindError

from spearspray.utils.constants import GREEN, RED, YELLOW, RESET


class Ldap:

    SCALAR_ATTRS = {
        "name", "sAMAccountName",  "whenCreated",
        "badPwdCount", "pwdLastSet", "msDS-ResultantPSO"
    }

    def __init__(self, target, domain, username, password, ssl_enabled, page_size):
        
        # TODO: Maybe add Context Manager for simplifying connection management
        
        self.target = target
        self.domain = domain
        self.username = username
        self.password = password
        self.use_ssl = ssl_enabled 
        self.page_size = page_size
        
        self.log = logging.getLogger(__name__)
        self.base_dn = self._get_basedn_from_domain()

    def _get_basedn_from_domain(self) -> str:
        """Convert domain name to Base DN format."""
        return ','.join(f'DC={part}' for part in self.domain.split('.'))

    def _create_connection(self, use_ssl: bool, insecure_ssl: bool, port: int) -> ldap3.Connection:
        """Create connection with specified parameters."""
        
        # Create server with specified SSL settings
        tls = None
        if use_ssl:
            validate = ssl.CERT_NONE if insecure_ssl else ssl.CERT_REQUIRED
            tls = ldap3.Tls(
                validate=validate,
                version=ssl.PROTOCOL_TLSv1_2,
                ciphers='ALL:@SECLEVEL=0',
            )

        server = ldap3.Server(
            host=self.target,
            port=port,
            use_ssl=use_ssl,
            tls=tls,
            get_info=ldap3.ALL,
            connect_timeout=5
        )

        # For LDAPS, use SIMPLE auth (avoids NTLM channel binding issues)
        # For plain LDAP, use NTLM
        if use_ssl:
            # SIMPLE auth over LDAPS - use UPN format (user@domain)
            user = f"{self.username}@{self.domain}"
            auth_method = SIMPLE
        else:
            # NTLM auth for plain LDAP
            user = f"{self.domain}\\{self.username}"
            auth_method = NTLM

        return ldap3.Connection(
            server=server,
            user=user,
            password=self.password,
            authentication=auth_method,
            auto_bind=True,
            raise_exceptions=True,
            auto_referrals=False
        )

    def _try_ldaps_strict(self) -> Optional[ldap3.Connection]:
        """Try LDAPS with strict certificate validation."""
        conn = self._create_connection(use_ssl=True, insecure_ssl=False, port=636)
        self.log.success(f"{GREEN}[+]{RESET} LDAPS (strict) - Login successful.")
        return conn

    def _try_ldaps_insecure(self) -> Optional[ldap3.Connection]:
        """Try LDAPS without certificate validation."""
        conn = self._create_connection(use_ssl=True, insecure_ssl=True, port=636)
        self.log.success(f"{GREEN}[+]{RESET} LDAPS (insecure) - Login successful.")
        return conn

    def _try_ldap_plain(self) -> Optional[ldap3.Connection]:
        """Try plain LDAP connection with NTLM."""
        conn = self._create_connection(use_ssl=False, insecure_ssl=False, port=389)
        self.log.success(f"{GREEN}[+]{RESET} LDAP - Login successful.")
        return conn

    def _try_ldaps_on_strong_auth_required(self) -> Optional[ldap3.Connection]:
        """Try LDAPS when strongAuthRequired was detected on plain LDAP."""
        self.log.info("[!] DC requires LDAP signing/sealing (strongAuthRequired). Trying LDAPS with SIMPLE auth.")
        try:
            return self._try_ldaps_strict()
        except ssl.SSLCertVerificationError:
            self.log.warning(f"{YELLOW}[!]{RESET} LDAPS (strict) failed certificate verification; trying insecure LDAPS.")
            return self._try_ldaps_insecure()

    @staticmethod
    def _is_stronger_auth_required(exc: LDAPBindError) -> bool:
        """Check if the error is strongAuthRequired."""
        error_str = str(exc).lower()
        if "strongerauthrequired" in error_str or "strongauthrequired" in error_str:
            return True
        try:
            if exc.args and isinstance(exc.args[0], dict):
                result_dict = exc.args[0]
                code = result_dict.get("result")
                desc = (result_dict.get("description") or "").lower()
                return code == 8 or "strongauthrequired" in desc
        except Exception:
            pass
        return False

    def connect_via_credentials(self) -> Optional[ldap3.Connection]:
        """Connect to LDAP server with automatic fallback strategies."""
        
        # If SSL explicitly requested, try LDAPS strategies first
        if self.use_ssl:
            try:
                return self._try_ldaps_strict()
            except ssl.SSLCertVerificationError:
                self.log.warning(f"{YELLOW}[!]{RESET} LDAPS certificate verification failed, trying insecure mode.")
                try:
                    return self._try_ldaps_insecure()
                except LDAPBindError as e:
                    self.log.error(f"{RED}[-]{RESET} LDAPS connection failed: {e}")
                    return None
            except LDAPBindError as e:
                self.log.error(f"{RED}[-]{RESET} LDAPS connection failed: {e}")
                return None
            except Exception as e:
                self.log.exception(f"{RED}[-]{RESET} Unexpected error during LDAPS connection.")
                return None

        # Try plain LDAP first, fallback to LDAPS if strongAuthRequired
        try:
            return self._try_ldap_plain()
        except LDAPBindError as exc:
            if self._is_stronger_auth_required(exc):
                try:
                    return self._try_ldaps_on_strong_auth_required()
                except LDAPBindError as e:
                    self.log.error(f"{RED}[-]{RESET} LDAPS fallback failed: {e}")
                    return None
                except Exception as e:
                    self.log.exception(f"{RED}[-]{RESET} Unexpected error during LDAPS fallback.")
                    return None
            else:
                self.log.error(f"{RED}[-]{RESET} LDAP connection failed: {exc}")
                return None
        except Exception as e:
            self.log.exception(f"{RED}[-]{RESET} Unexpected error during LDAP connection.")
                

    def close_connection(self, ldap_connection: ldap3.Connection) -> None:
        """Close the LDAP connection if it is currently bound."""
        if ldap_connection and ldap_connection.bound:
            ldap_connection.unbind()
            self.log.debug(f"[+] LDAP connection closed")
        else:
            self.log.warning(f"{YELLOW}[!]{RESET} No active LDAP connection to close")

    def _normalize(self, attributes: dict) -> dict:
        """Normalize LDAP attributes: convert bytes to strings and extract scalars."""
        normalized_attributes = {}

        for attribute_name, attribute_value in attributes.items():
            # Convert bytes to strings to prevent variable_resolver failures
            # LDAP3 sometimes returns byte values that need string conversion
            if isinstance(attribute_value, bytes):
                attribute_value = attribute_value.decode("utf-8", "ignore")
            elif isinstance(attribute_value, list) and attribute_value and isinstance(attribute_value[0], bytes):
                attribute_value = [item.decode("utf-8", "ignore") for item in attribute_value]

            # Extract scalar values from lists for known scalar attributes
            if attribute_name in self.SCALAR_ATTRS and isinstance(attribute_value, list):
                if len(attribute_value) > 1:
                    self.log.debug(f"{YELLOW}[*]{RESET} {len(attribute_value)} values found for scalar attribute '{attribute_name}', using first value: {attribute_value[0]}")
                attribute_value = attribute_value[0] if attribute_value else None

            normalized_attributes[attribute_name] = attribute_value  # Keep original format

        return normalized_attributes

    def search(self, ldap_connection: ldap3.Connection, ldap_filter: str, attributes: Iterable[str]) -> List[Dict]:
        """Retrieve LDAP entries using server-side paging."""
        try:
            entry_generator = ldap_connection.extend.standard.paged_search(
                search_base=self.base_dn,
                search_filter=ldap_filter,
                attributes=list(attributes),
                paged_size=self.page_size,
                generator=True,  # Returns iterator instead of loading all results
            )

            normalized_results: List[Dict] = []
            current_page, total_entries = 0, 0

            for entry in entry_generator:
                # Filter out non-entry responses (referrals, search continuations, etc.)
                # Only process actual directory entries
                if entry["type"] != "searchResEntry":
                    continue

                # Normalize LDAP attributes (handle bytes, extract scalars)
                normalized_entry = self._normalize(entry["attributes"])
                normalized_results.append(normalized_entry)
                total_entries += 1

                # Track pagination progress using RFC 2696 control presence
                # OID 1.2.840.113556.1.4.319 indicates paged results control
                if ("controls" in entry and "1.2.840.113556.1.4.319" in entry["controls"]):
                    current_page += 1
                    self.log.debug(f"[+] Page {current_page} processed. {total_entries} objects collected so far")

            if not normalized_results:
                self.log.warning(f"{RED}[-]{RESET} LDAP search returned no results")
            else:
                self.log.debug(f"[+] Retrieved {total_entries} objects (page_size={self.page_size})")
            return normalized_results

        except LDAPException:
            self.log.exception("[-] LDAP search error")

    @staticmethod
    def _filetime_to_timedelta(value) -> timedelta:
        """Convert Windows FILETIME values to Python timedelta objects."""
        if isinstance(value, timedelta):
            return abs(value)  # Ensure positive duration
        
        # Convert raw FILETIME (100ns intervals) to seconds, then to timedelta
        # Divide by 10,000,000 to convert from 100ns to seconds
        return timedelta(seconds=abs(int(value)) / 10_000_000)

    def get_default_password_policy(self, ldap_connection: ldap3.Connection) -> dict:
        """Get default domain password policy."""

        attrs = [
            "minPwdLength", "maxPwdAge", "minPwdAge",
            "pwdHistoryLength", "pwdProperties",
            "lockoutThreshold", "lockoutDuration", "lockOutObservationWindow",
        ]

        # Search for domain root object (contains default password policy)
        ldap_connection.search(
            search_base=self.base_dn,
            search_filter="(objectClass=domain)",
            search_scope=ldap3.BASE, # Base DN search
            attributes=attrs,
        )
        
        if not ldap_connection.entries:
            raise RuntimeError("Domain object not found")

        # Extract domain object and create helper function for time conversion
        e = ldap_connection.entries[0]
        tf = self._filetime_to_timedelta
        
        # Return normalized policy with human-readable time units
        return {
            "minPwdLength": int(e.minPwdLength.value),
            "maxPwdAge_days": tf(e.maxPwdAge.value).days,
            "minPwdAge_hours": tf(e.minPwdAge.value).seconds // 3600,
            "pwdHistoryLength": int(e.pwdHistoryLength.value),
            "pwdProperties": int(e.pwdProperties.value),
            "lockoutThreshold": int(e.lockoutThreshold.value),
            "lockoutDuration_minutes": tf(e.lockoutDuration.value).seconds // 60,
            "lockOutObservationWindow_minutes": tf(
                e.lockOutObservationWindow.value
            ).seconds // 60,
        }
