import argparse
import logging
import sys
from importlib.resources import files

import argcomplete
from colorama import init

from spearspray.core import SpearSpray
from spearspray.utils.constants import BANNER, AUTHOR, RED, YELLOW, BOLD, RESET, DEFAULT_LDAP_USERS_QUERY
from spearspray.utils.variables_utils import (
    print_registered_variables
)
from spearspray.modules.logger import Logger

def parse_arguments():

    ldap_parser = argparse.ArgumentParser(add_help=False)
    ldap_group = ldap_parser.add_argument_group('LDAP Configuration', 'Configuration for LDAP connection')
    ldap_group.add_argument('-d', '--domain', help='LDAP domain name (e.g., fabrikam.local) (required).')
    ldap_group.add_argument('-u', '--username', help='LDAP username (required).')
    ldap_group.add_argument('-p', '--password', help='LDAP password (required).')
    ldap_group.add_argument('-dc', '--domain-controller', type=str, metavar="DOMAIN_CONTROLLER", help='FQDN (recommended) or IP address of the domain controller (required).')
    ldap_group.add_argument('-q', '--query', default=DEFAULT_LDAP_USERS_QUERY, help='Custom LDAP query to retrieve users for spraying.')
    ldap_group.add_argument('--ssl', action='store_true', help='Use SSL connection for LDAP. Also known as LDAPS (default: False).' )
    ldap_group.add_argument('-lps', '--ldap-page-size', type=int, default=200, help='LDAP paging size for large result sets (default: 200).')

    neo4j_parser = argparse.ArgumentParser(add_help=False)
    neo4j_group = neo4j_parser.add_argument_group('Neo4j Configuration', 'Configuration for Neo4j connection. It will be use to mark users as owned')
    neo4j_group.add_argument('-nu', '--neo4j-username', help='Neo4j username (optional).')
    neo4j_group.add_argument('-np', '--neo4j-password', help='Neo4j password (optional).')
    neo4j_group.add_argument('-uri', '--uri', default='bolt://localhost:7687', help='Neo4j URI (default: bolt://localhost:7687).')

    sprayingconfig_parser = argparse.ArgumentParser(add_help=False)
    sprayingconfig_group = sprayingconfig_parser.add_argument_group('Password spraying configuration', 'Configuration for stealthy and controlled password spraying')
    sprayingconfig_group.add_argument('-t', '--threads', type=int, default=10, help='Maximum number of concurrent authentication threads (default: 10).')
    sprayingconfig_group.add_argument('-j', '--jitter', type=str, metavar="INTERVAL", default=0, help='Per-thread delay: N seconds fixed or N,M seconds random (default: 0).')
    sprayingconfig_group.add_argument('--max-rps', type=float, default=None, help='Maximum Kerberos requests per second (RPS). If not set, no rate limiting is applied.')
    sprayingconfig_group.add_argument('-thr', '--threshold', type=int, default=2, help='Number of password attempts left before stopping the spraying process (default: 2).')

    patterns_parser = argparse.ArgumentParser(add_help=False)
    patterns_group = patterns_parser.add_argument_group('Patterns', 'Configuration for patterns used in spraying')
    patterns_group.add_argument('-i', '--input', type=str, default=str(files("spearspray").joinpath("patterns.txt")), help='Patterns file (default: patterns.txt).')
    patterns_group.add_argument('-x', '--extra', type=str, default=None, help='Single word (no spaces or commas).')
    patterns_group.add_argument('-sep', '--separator', type=str, default=None, help='Separator for patterns.')
    patterns_group.add_argument('-suf', '--suffix', type=str, default=None, help='Suffix for patterns.')
    
    # Main parser
    parser = argparse.ArgumentParser(
        prog='spearspray',
        description=(
            f"{BANNER}\n"
            f"{BOLD}> Password spraying against Active Directory leveraging user data.\n{RESET}"
            f"{BOLD}> Author: {AUTHOR}{RESET}"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        parents=[ldap_parser, neo4j_parser, sprayingconfig_parser, patterns_parser]
    )

    parser.add_argument('-s', '--silent', action='store_true', help='Do not display the startup banner')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('-l', '--list', action='store_true', required=False, help='List all available variables and their descriptions.')

    argcomplete.autocomplete(parser)

    return parser

def check_required_args(args, parser):
    REQUIRED_ARGS = [
        ("username", "-u/--username"),
        ("password", "-p/--password"),
        ("domain", "-d/--domain"),
        ("domain_controller", "-dc/--domain-controller"),
    ]

    missing = []
    for attr, flag in REQUIRED_ARGS:
        if not getattr(args, attr, None):
            missing.append(flag)

    if missing:
        parser.error(
            f"{YELLOW}[*] Missing required arguments: {' '.join(missing)}{RESET}"
        )


def check_args(args, parser):

    if not args.silent:
        print(f"{RED}{BANNER}{RESET}")

    if args.list:
        print_registered_variables()
        return True

    check_required_args(args, parser)

    if args.threads < 1:
        parser.error(f"{YELLOW}[*] The number of threads must be at least 1{RESET}")

    if args.jitter:
        try:
            value = args.jitter
            parts = value.split(",")
            if len(parts) == 1:
                seconds = float(parts[0])
                args.jitter = (seconds, seconds)
            elif len(parts) == 2:
                min_seconds, max_seconds = map(float, parts)
                args.jitter = (min(min_seconds, max_seconds), max(min_seconds, max_seconds)) # Ensure min is less than or equal to max
            else:
                raise ValueError
        except ValueError:
            parser.error(f"{YELLOW}[*] Invalid jitter format. Use a single number or two numbers separated by a comma (e.g., 0.5 or 0.5,1.5){RESET}")
    else:
        args.jitter = (0.0, 0.0)

    if args.ldap_page_size and (args.ldap_page_size < 1 or args.ldap_page_size > 1000):
        parser.error(f"{YELLOW}[*] The --ldap-page-size (-lps) argument must be between 1 and 1000{RESET}")

    if args.extra and (',' in args.extra or ' ' in args.extra):
        parser.error(f"{YELLOW}[*] The --extra (-x) argument must be a single word (no spaces or commas){RESET}")
        return True

def cli():

    init(autoreset=True)
    
    parser = parse_arguments()
    args = parser.parse_args()
    
    Logger(name="spearspray", verbose=args.debug).get_logger()
    
    if check_args(args, parser):
        return
    
    spearspray = SpearSpray(args)
    spearspray.run()

if __name__ == '__main__':
    try:
        cli()
    except ValueError as e:
        logging.getLogger(__name__).error(f"{RED}[!]{RESET} Value error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning(f"{RED}[!]{RESET} Execution interrupted by user.")
        sys.exit(1)
    except Exception:
        logging.getLogger(__name__).exception(f"{RED}[!]{RESET} Unexpected error occurred.")
        sys.exit(1)
