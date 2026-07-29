// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title ServiceRegistry
/// @notice A public, priced catalog of machine-callable services on Arc.
/// @dev MoonWalk's agent used to read its priced tool list out of one app's
///      database, which means only that app can see it and nobody can audit what
///      a price was when a payment settled. Here the catalog is on-chain, so any
///      agent can discover a service, any user can check the price that was
///      advertised, and the approval that makes a service buyable is a public act.
///
///      Namespaces keep it multi-tenant. A namespace is a hashed community, in
///      MoonWalk keccak256("discord:<guildId>"). Anyone may list into a
///      namespace, but a listing is inert until that namespace's admin verifies
///      it, and any price change drops the verification, so an admin can never be
///      surprised by a price they did not approve.
contract ServiceRegistry {
    struct Service {
        bytes32 namespace;
        address lister;
        address payTo;
        address asset;
        uint256 priceAtomic;
        bool verified;
        bool enabled;
        string name;
        string description;
        string endpoint;
    }

    uint256 public constant MAX_NAME = 40;
    uint256 public constant MAX_DESCRIPTION = 200;
    uint256 public constant MAX_ENDPOINT = 400;

    mapping(bytes32 => address) public namespaceAdmin;
    /// @notice Ceiling a namespace admin puts on any listing there. 0 means none.
    mapping(bytes32 => uint256) public namespaceMaxPrice;
    mapping(bytes32 => Service) private _services;
    mapping(bytes32 => bytes32[]) private _ids;

    event NamespaceClaimed(bytes32 indexed namespace, address indexed admin);
    event NamespaceTransferred(bytes32 indexed namespace, address indexed from, address indexed to);
    event MaxPriceSet(bytes32 indexed namespace, uint256 maxPriceAtomic);
    event ServiceRegistered(
        bytes32 indexed id,
        bytes32 indexed namespace,
        address indexed lister,
        string name,
        uint256 priceAtomic,
        address payTo,
        address asset,
        string endpoint
    );
    event ServicePriced(bytes32 indexed id, uint256 priceAtomic, address payTo);
    event ServiceVerified(bytes32 indexed id, address indexed admin, bool verified);
    event ServiceEnabled(bytes32 indexed id, bool enabled);

    error NamespaceTaken();
    error NotNamespaceAdmin();
    error NotLister();
    error ServiceExists();
    error UnknownService();
    error ZeroAddressField();
    error ZeroPrice();
    error PriceAboveNamespaceMax(uint256 priceAtomic, uint256 maxPriceAtomic);
    error NameLength();
    error DescriptionLength();
    error EndpointLength();
    error EndpointNotHttps();

    /// @notice Deterministic service id so an off-chain catalog can address a
    ///         listing without reading the chain first.
    function serviceIdOf(bytes32 namespace, string calldata name) public pure returns (bytes32) {
        return keccak256(abi.encode(namespace, keccak256(bytes(name))));
    }

    // ---- namespaces -------------------------------------------------------

    function claimNamespace(bytes32 namespace) external {
        if (namespaceAdmin[namespace] != address(0)) revert NamespaceTaken();
        namespaceAdmin[namespace] = msg.sender;
        emit NamespaceClaimed(namespace, msg.sender);
    }

    function transferNamespace(bytes32 namespace, address newAdmin) external {
        _requireAdmin(namespace);
        if (newAdmin == address(0)) revert ZeroAddressField();
        namespaceAdmin[namespace] = newAdmin;
        emit NamespaceTransferred(namespace, msg.sender, newAdmin);
    }

    function setMaxPrice(bytes32 namespace, uint256 maxPriceAtomic) external {
        _requireAdmin(namespace);
        namespaceMaxPrice[namespace] = maxPriceAtomic;
        emit MaxPriceSet(namespace, maxPriceAtomic);
    }

    // ---- listings ---------------------------------------------------------

    /// @notice List a priced service. It is not buyable until an admin verifies it.
    function register(
        bytes32 namespace,
        string calldata name,
        string calldata description,
        string calldata endpoint,
        address payTo,
        address asset,
        uint256 priceAtomic
    ) external returns (bytes32 id) {
        if (payTo == address(0) || asset == address(0)) revert ZeroAddressField();
        if (priceAtomic == 0) revert ZeroPrice();
        uint256 nameLen = bytes(name).length;
        if (nameLen < 3 || nameLen > MAX_NAME) revert NameLength();
        if (bytes(description).length > MAX_DESCRIPTION) revert DescriptionLength();
        _requireHttps(endpoint);
        _requireUnderMax(namespace, priceAtomic);

        id = serviceIdOf(namespace, name);
        if (_services[id].lister != address(0)) revert ServiceExists();

        _services[id] = Service({
            namespace: namespace,
            lister: msg.sender,
            payTo: payTo,
            asset: asset,
            priceAtomic: priceAtomic,
            verified: false,
            enabled: true,
            name: name,
            description: description,
            endpoint: endpoint
        });
        _ids[namespace].push(id);
        emit ServiceRegistered(id, namespace, msg.sender, name, priceAtomic, payTo, asset, endpoint);
    }

    /// @notice Change the price or the payout address.
    /// @dev Drops verification, so what an admin approved is what stays buyable.
    function setPrice(bytes32 id, uint256 priceAtomic, address payTo) external {
        Service storage s = _requireLister(id);
        if (payTo == address(0)) revert ZeroAddressField();
        if (priceAtomic == 0) revert ZeroPrice();
        _requireUnderMax(s.namespace, priceAtomic);
        s.priceAtomic = priceAtomic;
        s.payTo = payTo;
        emit ServicePriced(id, priceAtomic, payTo);
        if (s.verified) {
            s.verified = false;
            emit ServiceVerified(id, msg.sender, false);
        }
    }

    /// @notice Namespace admin approves or revokes a listing.
    function setVerified(bytes32 id, bool verified) external {
        Service storage s = _service(id);
        _requireAdmin(s.namespace);
        s.verified = verified;
        emit ServiceVerified(id, msg.sender, verified);
    }

    /// @notice Lister or namespace admin can take a listing out of service.
    function setEnabled(bytes32 id, bool enabled) external {
        Service storage s = _service(id);
        if (msg.sender != s.lister && msg.sender != namespaceAdmin[s.namespace]) revert NotLister();
        s.enabled = enabled;
        emit ServiceEnabled(id, enabled);
    }

    // ---- views ------------------------------------------------------------

    function getService(bytes32 id) external view returns (Service memory) {
        return _service(id);
    }

    /// @notice Every listing id in a namespace, verified or not.
    function idsOf(bytes32 namespace) external view returns (bytes32[] memory) {
        return _ids[namespace];
    }

    function countOf(bytes32 namespace) external view returns (uint256) {
        return _ids[namespace].length;
    }

    /// @notice What an agent should check before paying: approved and switched on.
    function isBuyable(bytes32 id) external view returns (bool) {
        Service memory s = _services[id];
        return s.lister != address(0) && s.verified && s.enabled;
    }

    // ---- internals --------------------------------------------------------

    function _service(bytes32 id) private view returns (Service storage s) {
        s = _services[id];
        if (s.lister == address(0)) revert UnknownService();
    }

    function _requireLister(bytes32 id) private view returns (Service storage s) {
        s = _service(id);
        if (s.lister != msg.sender) revert NotLister();
    }

    function _requireAdmin(bytes32 namespace) private view {
        if (namespaceAdmin[namespace] != msg.sender) revert NotNamespaceAdmin();
    }

    function _requireUnderMax(bytes32 namespace, uint256 priceAtomic) private view {
        uint256 max = namespaceMaxPrice[namespace];
        if (max != 0 && priceAtomic > max) revert PriceAboveNamespaceMax(priceAtomic, max);
    }

    /// @dev Cheap prefix check. A registry other agents read should not advertise
    ///      a plaintext or non-http endpoint.
    function _requireHttps(string calldata endpoint) private pure {
        bytes calldata b = bytes(endpoint);
        if (b.length < 12 || b.length > MAX_ENDPOINT) revert EndpointLength();
        // The cast is safe, the slice is exactly 8 bytes and the literal is the
        // same length, so nothing is truncated.
        // forge-lint: disable-next-line(unsafe-typecast)
        if (bytes8(b[:8]) != bytes8("https://")) revert EndpointNotHttps();
    }
}
