// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title SpendGuard
/// @notice Per-subject spend caps enforced on-chain.
/// @dev A subject is whoever a payer spent on behalf of, hashed off-chain. In
///      MoonWalk a subject is keccak256("discord:<guildId>:<userId>"), so each
///      person in a shared channel has their own cap while the agent spends from
///      one wallet.
///
///      The point is where the rule lives. A backend budget is a promise: trust
///      the operator not to overspend you. A cap here is a rule in the contract,
///      so a voucher that would push a subject past its cap cannot be redeemed
///      by anyone, the operator included.
///
///      The guard is not tied to one channel. Scopes are namespaced by the
///      calling contract, so any payer contract can register scopes and consume
///      against them, and two apps can never touch each other's usage.
contract SpendGuard {
    struct Cap {
        uint256 limit; // atomic USDC allowed per window
        uint64 window; // seconds; 0 means the limit is a lifetime total
        bool set;
    }

    struct Usage {
        uint256 used;
        uint64 windowStart;
    }

    /// @notice app => scope => the account allowed to set caps for that scope.
    mapping(bytes32 => address) private _scopeOwner;
    mapping(bytes32 => Cap) private _defaultCap;
    mapping(bytes32 => mapping(bytes32 => Cap)) private _subjectCap;
    mapping(bytes32 => mapping(bytes32 => Usage)) private _usage;

    event ScopeRegistered(address indexed app, bytes32 indexed scope, address indexed owner);
    event DefaultCapSet(address indexed app, bytes32 indexed scope, uint256 limit, uint64 window);
    event SubjectCapSet(
        address indexed app, bytes32 indexed scope, bytes32 indexed subject, uint256 limit, uint64 window
    );
    event Consumed(
        address indexed app, bytes32 indexed scope, bytes32 indexed subject, uint256 amount, uint256 used, uint256 limit
    );

    error ScopeTaken();
    error NotScopeOwner();
    error ZeroOwner();
    error NotConfigured(address app, bytes32 scope, bytes32 subject);
    error CapExceeded(bytes32 subject, uint256 used, uint256 amount, uint256 limit);

    /// @notice Claim a scope for `owner`. Called by the payer contract, once per
    ///         scope, so scope ownership is authoritative instead of first come.
    function registerScope(bytes32 scope, address owner) external {
        if (owner == address(0)) revert ZeroOwner();
        bytes32 key = _key(msg.sender, scope);
        if (_scopeOwner[key] != address(0)) revert ScopeTaken();
        _scopeOwner[key] = owner;
        emit ScopeRegistered(msg.sender, scope, owner);
    }

    /// @notice Cap that applies to any subject without its own cap.
    /// @dev Setting a limit of 0 blocks every unnamed subject, which is the safe
    ///      default for a scope that only serves a known list of people.
    function setDefaultCap(address app, bytes32 scope, uint256 limit, uint64 window) external {
        bytes32 key = _requireOwner(app, scope);
        _defaultCap[key] = Cap({limit: limit, window: window, set: true});
        emit DefaultCapSet(app, scope, limit, window);
    }

    /// @notice Cap for one subject, overriding the scope default.
    function setSubjectCap(address app, bytes32 scope, bytes32 subject, uint256 limit, uint64 window)
        external
    {
        bytes32 key = _requireOwner(app, scope);
        _subjectCap[key][subject] = Cap({limit: limit, window: window, set: true});
        emit SubjectCapSet(app, scope, subject, limit, window);
    }

    /// @notice Book `amount` against a subject, reverting if it breaks the cap.
    /// @dev Callable by the payer contract only for its own scopes: the app is
    ///      msg.sender, so no caller can spend another app's allowance. Fails
    ///      closed, an unconfigured scope cannot spend anything.
    function consume(bytes32 scope, bytes32 subject, uint256 amount) external {
        bytes32 key = _key(msg.sender, scope);
        Cap memory cap = _resolve(key, subject);
        if (!cap.set) revert NotConfigured(msg.sender, scope, subject);

        Usage storage u = _usage[key][subject];
        uint256 used = u.used;
        if (cap.window != 0 && block.timestamp >= uint256(u.windowStart) + cap.window) {
            used = 0;
            u.windowStart = uint64(block.timestamp);
        } else if (u.windowStart == 0) {
            u.windowStart = uint64(block.timestamp);
        }

        uint256 next = used + amount;
        if (next > cap.limit) revert CapExceeded(subject, used, amount, cap.limit);
        u.used = next;
        emit Consumed(msg.sender, scope, subject, amount, next, cap.limit);
    }

    // ---- views ------------------------------------------------------------

    function scopeOwner(address app, bytes32 scope) external view returns (address) {
        return _scopeOwner[_key(app, scope)];
    }

    /// @notice The cap that would apply to this subject right now.
    function capOf(address app, bytes32 scope, bytes32 subject)
        external
        view
        returns (uint256 limit, uint64 window, bool set)
    {
        Cap memory cap = _resolve(_key(app, scope), subject);
        return (cap.limit, cap.window, cap.set);
    }

    /// @notice Spend booked in the current window, and when that window opened.
    function usageOf(address app, bytes32 scope, bytes32 subject)
        external
        view
        returns (uint256 used, uint64 windowStart)
    {
        Usage memory u = _usage[_key(app, scope)][subject];
        return (u.used, u.windowStart);
    }

    /// @notice What this subject can still spend, treating an expired window as
    ///         already rolled over. Returns 0 for an unconfigured scope.
    function remaining(address app, bytes32 scope, bytes32 subject) external view returns (uint256) {
        bytes32 key = _key(app, scope);
        Cap memory cap = _resolve(key, subject);
        if (!cap.set) return 0;
        Usage memory u = _usage[key][subject];
        if (cap.window != 0 && block.timestamp >= uint256(u.windowStart) + cap.window) {
            return cap.limit;
        }
        return u.used >= cap.limit ? 0 : cap.limit - u.used;
    }

    // ---- internals --------------------------------------------------------

    function _key(address app, bytes32 scope) private pure returns (bytes32) {
        return keccak256(abi.encode(app, scope));
    }

    function _requireOwner(address app, bytes32 scope) private view returns (bytes32 key) {
        key = _key(app, scope);
        if (_scopeOwner[key] != msg.sender) revert NotScopeOwner();
    }

    function _resolve(bytes32 key, bytes32 subject) private view returns (Cap memory) {
        Cap memory cap = _subjectCap[key][subject];
        if (cap.set) return cap;
        return _defaultCap[key];
    }
}
