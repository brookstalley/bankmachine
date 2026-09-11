"""Configuration: documented defaults, no hardcoded filesystem paths (AC-ARCH.4).

Every path this product touches is a configuration value resolved here. Nothing
else in the codebase may join a path onto a literal directory, and no code path
assumes the repository's own location -- the product is installed and run from
anywhere, and the operator's data does not live beside its source.

Precedence, highest first: an explicit argument, an environment variable, the
config file, then the documented default.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast, get_args

APP_NAME: Final = "bankmachine"

Environment = Literal["sandbox", "production"]
ENVIRONMENTS: Final[tuple[str, ...]] = get_args(Environment)

#: Where a resolved value came from. 🔴 `"default"` is the only one that means
#: *nobody said*: an explicit argument, an exported variable and a config-file
#: entry are all somebody choosing, and the guard below cares about that
#: distinction and about nothing else.
ValueSource = Literal["argument", "environment variable", "config file", "default"]

ENV_PREFIX: Final = "BANKMACHINE_"

#: 🔴 The largest history window the aggregator will grant, and the one this
#: product asks for unless told otherwise.
#:
#: 730 days is the aggregator's own inclusive maximum *(read off the SDK's
#: request model rather than recalled: `plaid/model/link_token_transactions.py`
#: declares `inclusive_maximum: 730, inclusive_minimum: 1`)*.
#:
#: **The default is the maximum on purpose.** AC-1.2 makes this immutable after
#: enrollment: getting it wrong means re-linking every institution, and the
#: requirements call a vendor-default build a failed build. The two ways to be
#: wrong are not symmetric -- asking for more history than needed costs nothing
#: and can be ignored, while asking for less costs history that cannot be
#: recovered at any price. So the default errs in the direction that is
#: reversible.
MAX_HISTORY_DAYS: Final = 730

#: How many live connections the aggregator plan allows. 🔴 AC-1.5 requires this
#: to be configuration and not a literal, because plan tiers change -- and the
#: refusal has to come from here rather than from the aggregator, which would
#: reject only *after* the operator had completed a Link session and minted an
#: Item nobody can use.
DEFAULT_CONNECTION_CAP: Final = 10

#: SQLite waits this long for a competing writer before raising `database is
#: locked`. It is the second layer only; writer processes serialize on the
#: advisory lock in `store.connection` before they open anything.
DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000


class ConfigError(Exception):
    """The configuration could not be resolved, or resolved to something invalid."""


class UnchosenEnvironmentError(ConfigError):
    """A command that writes per-environment state ran on an environment nobody chose.

    🔴 A `ConfigError` subclass so that it inherits the existing exit-2 mapping
    rather than picking a code at the call site. `2` is "could not run", which is
    exactly what this is: the command is well-formed and the machine is healthy,
    but the one value that decides *which* of two containers the write lands in
    was never supplied by anyone.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved configuration. Every field is absolute by the time it is here."""

    environment: Environment
    datastore_path: Path
    log_dir: Path
    keychain_service: str
    busy_timeout_ms: int
    plaid_client_id: str | None
    """The aggregator client identifier. Public in the sense that it is not the secret,
    but still an operator's own value, so it is configuration rather than a literal."""
    connection_cap: int
    """How many live connections may exist at once (AC-1.5).

    Configuration rather than a literal because plan tiers change. Retired
    connections do not count against it: they hold history and cost nothing at
    the aggregator.
    """
    history_days: int
    """How much transaction history enrollment asks the aggregator to grant.

    🔴 Immutable per connection once that connection is enrolled (AC-1.2). This
    value is read at enrollment and never again, so changing it later moves
    nothing that already exists -- it only changes what the *next* enrollment
    asks for.
    """
    config_path: Path | None
    """The file the values came from, or None when nothing but defaults and env applied."""
    environment_source: ValueSource = "argument"
    """Who chose `environment`.

    🔴 Defaults to `"argument"` because constructing a Config directly IS
    choosing one -- the caller named the environment in the constructor. Only
    `load_config`'s fallback produces `"default"`, which is the single state
    `require_chosen_environment` refuses.
    """

    @property
    def environment_chosen(self) -> bool:
        """Whether anybody actually selected this environment.

        Every per-environment container -- `datastore:<env>` and `plaid:<env>` in
        the keychain, `connection:<env>:<id>`, and the datastore filename itself
        -- is keyed on `environment`. When nothing chose it, a write does not go
        somewhere harmless; it goes to whichever container the fallback names,
        which is indistinguishable at the keychain from the operator meaning it.
        """
        return self.environment_source != "default"

    @property
    def lock_path(self) -> Path:
        """The advisory writer lock, beside the datastore it guards."""
        return self.datastore_path.with_name(self.datastore_path.name + ".lock")

    @property
    def keychain_account(self) -> str:
        """The keychain account holding the datastore key for THIS environment.

        Sandbox and production have separate datastores, so they have separate
        keys; one account name for both would key the production store with a
        credential the operator believes is disposable.

        The word "key" is deliberately absent: the redacting log formatter
        treats `key:<value>` as a credential and would blank the environment
        out of every line naming this account.
        """
        return f"datastore:{self.environment}"

    @property
    def plaid_keychain_account(self) -> str:
        """The keychain account holding the aggregator secret for THIS environment.

        Separate from the datastore key's account for the same reason it is
        separate from production's: a sandbox secret is disposable and a
        production one is not, and one account name for both would let an
        operator overwrite the second while believing they were replacing the
        first.
        """
        return f"plaid:{self.environment}"

    def connection_keychain_account(self, source_connection_id: str) -> str:
        """The keychain account holding ONE connection's access token.

        Environment-scoped for the reason the two above are: a sandbox item and a
        production item can carry the same aggregator id, and one account name for
        both would let a sandbox re-enrollment overwrite the credential for a real
        connection -- silently, and discoverable only at the next sync.

        The word "token" is deliberately absent for the same reason the datastore
        account avoids "key": the redacting formatter treats `token:<value>` as a
        credential and would blank the connection out of every line naming it.
        """
        return f"connection:{self.environment}:{source_connection_id}"


def _home(env: Mapping[str, str]) -> Path:
    """The home directory, through the injected environment.

    `Path.home()` reads the process environment directly, which would make the
    `env` argument a seam that stops one step short of the branch it is most
    needed for: the XDG *fallback* is the documented macOS default, and a test
    that passes HOME without this would silently resolve against the developer's
    own home while reading as isolated.
    """
    raw = env.get("HOME", "").strip()
    return Path(raw) if raw else Path.home()


def _base_dir(env: Mapping[str, str], var: str, *fallback: str) -> Path:
    """An XDG base directory, or its documented per-user fallback.

    macOS has no XDG convention of its own; honouring the variables when they
    are set and falling back to the Linux defaults keeps one rule for both, and
    the operator can always override the resolved path directly.
    """
    raw = env.get(var, "").strip()
    if raw:
        return Path(raw).expanduser()
    return _home(env).joinpath(*fallback)


def _default_datastore_name(environment: Environment) -> str:
    """Sandbox and production default to different files.

    AC-10.6 requires that syncing fixture data into the real datastore be hard
    to do by accident. Distinct default paths mean the accident needs an
    explicit override rather than a forgotten flag.
    """
    return "store.db" if environment == "production" else "store-sandbox.db"


def _as_environment(value: str) -> Environment:
    """Narrow a configured string to the environment it claims to be."""
    if value not in ENVIRONMENTS:
        raise ConfigError(f"environment must be one of {', '.join(ENVIRONMENTS)}, got {value!r}")
    return cast(Environment, value)


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the config file is looked for when nothing says otherwise."""
    env = os.environ if env is None else env
    return _base_dir(env, "XDG_CONFIG_HOME", ".config") / APP_NAME / "config.toml"


def _read_config_file(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc


def _resolve_sourced(
    key: str,
    env: Mapping[str, str],
    file_values: Mapping[str, object],
) -> tuple[str | None, ValueSource]:
    """One value and where it came from, by precedence: environment, then config file.

    The sourced form is the real one and `_resolve` discards half of it, rather
    than the two walking the precedence separately: two implementations of one
    precedence order drift, and the one that drifts silently is the one nothing
    reads back.
    """
    from_env = env.get(ENV_PREFIX + key.upper(), "").strip()
    if from_env:
        return from_env, "environment variable"
    from_file = file_values.get(key)
    if from_file is None:
        return None, "default"
    if not isinstance(from_file, str | int):
        raise ConfigError(
            f"config key {key!r} must be a string or integer, got {type(from_file).__name__}"
        )
    return str(from_file), "config file"


def _resolve(
    key: str,
    env: Mapping[str, str],
    file_values: Mapping[str, object],
) -> str | None:
    """One value, by precedence: environment variable, then config file."""
    return _resolve_sourced(key, env, file_values)[0]


def display_path(path: Path, env: Mapping[str, str] | None = None) -> str:
    """A path with the operator's home elided.

    Here rather than beside its first caller because two surfaces need it and
    `config` is the one they both already depend on: the log formatter (logs get
    pasted into bug reports) and the refusals below (an error naming
    `/Users/<someone>/...` puts an account name on a terminal and in whatever
    captures it).

    🔴 Home comes from `_home`, the same seam the paths themselves resolve
    through, rather than from `Path.home()`. The two agree for the process
    environment, so nothing changes today -- but a config built from an injected
    `env=` would otherwise resolve its paths against one home and elide against
    another, which makes the elision the one thing in this module a test cannot
    prove through the seam it proves everything else through.
    """
    home = _home(os.environ if env is None else env)
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def require_chosen_environment(config: Config) -> None:
    """Refuse to write per-environment state on an environment nobody selected.

    🔴 Called from the two places that perform such writes -- the one writer
    factory in `store.connection` and the keychain mutators in `secrets` --
    rather than from a list of command names. Every per-environment container is
    reached through one of those two, so a command added later is covered
    without anyone remembering to add it; a guard written as an enumeration of
    commands is a guarantee that decays on the first one nobody lists.

    Reads are deliberately not guarded. `store status`, `connections list` and
    the MCP server keep answering under the fallback, because the hazard here is
    not *looking at* the wrong environment -- which is visible and free to
    correct -- but *writing* to it, which at the keychain is indistinguishable
    from having meant it, and for a secret is unrecoverable.
    """
    if config.environment_chosen:
        return
    raise UnchosenEnvironmentError(
        f"no environment was chosen, so {config.environment!r} was assumed -- and this "
        f"command writes state that belongs to one environment. Nothing was written. "
        f"Choose one, either for this shell:\n"
        f"    export {ENV_PREFIX}ENVIRONMENT={config.environment}\n"
        # Double-quoted explicitly rather than through `!r`: this line is meant
        # to be pasted into a TOML file, and Python's repr would hand over
        # single quotes -- legal TOML, but not what every example in the docs
        # shows, which is how a paste turns into a question.
        f"or once, by putting\n"
        f'    environment = "{config.environment}"\n'
        f"in {display_path(config.config_path or default_config_path())}"
    )


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    """Resolve configuration from arguments, environment, config file and defaults.

    `env` and `config_path` are injected rather than read globally so the suite
    can exercise real precedence instead of a mock of it.
    """
    env = os.environ if env is None else env

    if config_path is None:
        override = env.get(ENV_PREFIX + "CONFIG", "").strip()
        config_path = Path(override).expanduser() if override else default_config_path(env)
    explicit_config = config_path.exists()
    file_values = _read_config_file(config_path)

    environment_raw, environment_source = _resolve_sourced("environment", env, file_values)
    if not environment_raw:
        # 🔴 A key present but empty (`environment = ""`) resolves to the same
        # fallback as a key that is absent, so it is the same amount of choosing:
        # none. Without this, an empty entry would satisfy the write guard while
        # the value it selected came from nowhere -- the exact state the guard
        # exists to refuse, reached by a typo.
        environment_source = "default"
    environment = _as_environment(environment_raw or "sandbox")

    data_home = _base_dir(env, "XDG_DATA_HOME", ".local", "share") / APP_NAME
    state_home = _base_dir(env, "XDG_STATE_HOME", ".local", "state") / APP_NAME

    datastore_raw = _resolve("datastore_path", env, file_values)
    datastore_path = (
        Path(datastore_raw).expanduser()
        if datastore_raw
        else data_home / _default_datastore_name(environment)
    )

    log_raw = _resolve("log_dir", env, file_values)
    log_dir = Path(log_raw).expanduser() if log_raw else state_home / "logs"

    keychain_service = _resolve("keychain_service", env, file_values) or APP_NAME

    plaid_client_id = _resolve("plaid_client_id", env, file_values)

    history_raw = _resolve("history_days", env, file_values)
    try:
        history_days = int(history_raw) if history_raw else MAX_HISTORY_DAYS
    except ValueError as exc:
        raise ConfigError(f"history_days must be an integer, got {history_raw!r}") from exc
    if not 1 <= history_days <= MAX_HISTORY_DAYS:
        # Refused rather than clamped. A clamp would enroll a connection at a
        # window the operator did not choose and never told them -- and the
        # window is immutable afterwards, so the correction costs a re-link of
        # every institution.
        raise ConfigError(
            f"history_days must be between 1 and {MAX_HISTORY_DAYS} (the aggregator's own "
            f"maximum), got {history_days}"
        )

    cap_raw = _resolve("connection_cap", env, file_values)
    try:
        connection_cap = int(cap_raw) if cap_raw else DEFAULT_CONNECTION_CAP
    except ValueError as exc:
        raise ConfigError(f"connection_cap must be an integer, got {cap_raw!r}") from exc
    if connection_cap < 1:
        # A cap of zero would refuse every enrollment including the first, which
        # is indistinguishable from the product being broken. Refusing the value
        # names the setting instead.
        raise ConfigError(f"connection_cap must be at least 1, got {connection_cap}")

    timeout_raw = _resolve("busy_timeout_ms", env, file_values)
    try:
        busy_timeout_ms = int(timeout_raw) if timeout_raw else DEFAULT_BUSY_TIMEOUT_MS
    except ValueError as exc:
        raise ConfigError(f"busy_timeout_ms must be an integer, got {timeout_raw!r}") from exc
    if busy_timeout_ms < 0:
        raise ConfigError(f"busy_timeout_ms must not be negative, got {busy_timeout_ms}")

    return Config(
        environment=environment,
        datastore_path=datastore_path.absolute(),
        log_dir=log_dir.absolute(),
        keychain_service=keychain_service,
        busy_timeout_ms=busy_timeout_ms,
        plaid_client_id=plaid_client_id,
        connection_cap=connection_cap,
        history_days=history_days,
        config_path=config_path if explicit_config else None,
        environment_source=environment_source,
    )
