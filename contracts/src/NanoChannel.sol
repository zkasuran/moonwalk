// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IUSDC} from "./interfaces/IUSDC.sol";
import {SpendGuard} from "./SpendGuard.sol";

/// @title NanoChannel
/// @notice A unidirectional USDC payment channel for sub-cent metered calls on Arc.
/// @dev Why a channel. A per-call on-chain transfer costs more than a $0.001 call
///      is worth, so "nanopayments" that settle one transaction per call are
///      nanopayments in name only. Here the payer funds a channel once, then
///      signs an EIP-712 voucher per call off-chain. The service redeems the
///      newest voucher whenever it likes, and one transaction settles every call
///      since the last redeem.
///
///      Two things make it fit Arc specifically. Funding uses EIP-3009
///      receiveWithAuthorization, so the payer signs and never sends a
///      transaction, never approves, and needs no gas. And because gas on Arc is
///      USDC, the party that submits the redeem pays in the same asset it is
///      collecting.
///
///      Vouchers carry a subject, whoever the payer spent on behalf of. In
///      MoonWalk that is a hashed Discord user, so a shared agent wallet keeps
///      honest per-person accounting and SpendGuard can cap each person on-chain.
contract NanoChannel {
    struct Channel {
        address payer;
        address service;
        uint256 deposit;
        uint256 redeemed;
        uint64 closeAt; // 0 while open, else the timestamp the payer may withdraw
        bool guarded;
        bool settled;
    }

    /// @notice One off-chain promise: subject has consumed `cumulative` in total.
    /// @dev Cumulative, not per-call, so a lost voucher costs nothing and a
    ///      replayed voucher pays nothing.
    struct Voucher {
        bytes32 channelId;
        bytes32 subject;
        uint256 cumulative;
        uint64 validBefore;
    }

    /// @notice An EIP-3009 authorization signed by the payer.
    struct Authorization {
        address from;
        uint256 value;
        uint256 validAfter;
        uint256 validBefore;
        bytes32 nonce;
        bytes signature;
    }

    bytes32 public constant VOUCHER_TYPEHASH =
        keccak256("Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)");
    bytes32 public constant CLOSE_TYPEHASH = keccak256("Close(bytes32 channelId,uint256 redeemed)");
    bytes32 private constant _EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant _NAME_HASH = keccak256("MoonWalk NanoChannel");
    bytes32 private constant _VERSION_HASH = keccak256("1");
    /// @dev secp256k1n / 2. A signature with a higher s is the malleable twin of
    ///      a valid one, so it is rejected outright.
    uint256 private constant _HALF_ORDER =
        0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    IUSDC public immutable usdc;
    SpendGuard public immutable guard;
    /// @notice How long the service keeps the right to redeem after the payer
    ///         asks to close. Set once at deploy.
    uint64 public immutable challengeWindow;

    uint256 private immutable _cachedChainId;
    bytes32 private immutable _cachedDomainSeparator;

    mapping(bytes32 => Channel) private _channels;
    mapping(bytes32 => mapping(bytes32 => uint256)) private _subjectRedeemed;

    event ChannelOpened(
        bytes32 indexed channelId, address indexed payer, address indexed service, uint256 deposit, bool guarded
    );
    event ChannelToppedUp(bytes32 indexed channelId, uint256 amount, uint256 deposit);
    event VoucherRedeemed(bytes32 indexed channelId, bytes32 indexed subject, uint256 cumulative, uint256 delta);
    event Redeemed(bytes32 indexed channelId, address indexed service, uint256 total, uint256 voucherCount);
    event CloseRequested(bytes32 indexed channelId, uint64 closeAt);
    event ChannelClosed(bytes32 indexed channelId, uint256 refund, bool cooperative);

    error ChannelExists();
    error UnknownChannel();
    error ChannelSettled();
    error NotPayer();
    error ZeroService();
    error ZeroDeposit();
    error WrongPayer();
    error BadBatch();
    error WrongChannel();
    error VoucherExpired(bytes32 subject);
    error StaleVoucher(bytes32 subject, uint256 cumulative, uint256 already);
    error BadSignature();
    error Underfunded(uint256 available, uint256 requested);
    error TransferFailed();
    error AlreadyClosing();
    error ChallengeOpen(uint64 closeAt);
    error NotClosing();

    constructor(IUSDC usdc_, SpendGuard guard_, uint64 challengeWindow_) {
        usdc = usdc_;
        guard = guard_;
        challengeWindow = challengeWindow_;
        _cachedChainId = block.chainid;
        _cachedDomainSeparator = _buildDomainSeparator();
    }

    // ---- open and fund ----------------------------------------------------

    /// @notice Open a channel and fund it with a signed USDC authorization.
    /// @dev Anyone may submit this. The payer's EIP-3009 signature is what moves
    ///      the money, so the payer needs no gas and no prior approve. `guarded`
    ///      binds every future redeem to SpendGuard caps for this channel.
    /// @param capOwner Who administers this channel's caps. Pass the zero address
    ///        to make it the payer. A separate cap owner exists because a payer
    ///        that never sends a transaction cannot configure anything on-chain,
    ///        and delegating is safe: caps only ever restrict what may be
    ///        redeemed, and spending still needs the payer's own signature.
    function open(
        address service,
        bytes32 salt,
        bool guarded,
        address capOwner,
        Authorization calldata auth
    ) external returns (bytes32 channelId) {
        if (service == address(0)) revert ZeroService();
        if (auth.value == 0) revert ZeroDeposit();
        channelId = channelIdOf(auth.from, service, salt);
        Channel storage ch = _channels[channelId];
        if (ch.payer != address(0)) revert ChannelExists();

        ch.payer = auth.from;
        ch.service = service;
        ch.deposit = auth.value;
        ch.guarded = guarded;

        if (guarded) guard.registerScope(channelId, capOwner == address(0) ? auth.from : capOwner);
        _pull(auth);

        emit ChannelOpened(channelId, auth.from, service, auth.value, guarded);
    }

    /// @notice Add funds to an open channel with another signed authorization.
    function topUp(bytes32 channelId, Authorization calldata auth) external returns (uint256 deposit) {
        Channel storage ch = _channels[channelId];
        if (ch.payer == address(0)) revert UnknownChannel();
        if (ch.settled) revert ChannelSettled();
        if (auth.from != ch.payer) revert WrongPayer();
        if (auth.value == 0) revert ZeroDeposit();

        deposit = ch.deposit + auth.value;
        ch.deposit = deposit;
        _pull(auth);
        emit ChannelToppedUp(channelId, auth.value, deposit);
    }

    /// @dev EIP-3009 requires msg.sender == to, so this contract has to be the
    ///      caller. The balance check is defensive: USDC reverts on a bad
    ///      authorization, and this catches a token that silently no-ops.
    function _pull(Authorization calldata auth) private {
        uint256 before = usdc.balanceOf(address(this));
        usdc.receiveWithAuthorization(
            auth.from, address(this), auth.value, auth.validAfter, auth.validBefore, auth.nonce, auth.signature
        );
        if (usdc.balanceOf(address(this)) - before != auth.value) revert TransferFailed();
    }

    // ---- redeem -----------------------------------------------------------

    /// @notice Settle one batch of vouchers, paying the service in a single transfer.
    /// @dev Anyone may submit, the money always goes to the channel's service, so
    ///      a service can hand the batch to a relayer. Each voucher is the newest
    ///      cumulative total for one subject; the delta over what that subject
    ///      already settled is what gets paid. A stale or replayed voucher
    ///      reverts rather than paying twice.
    function redeem(bytes32 channelId, Voucher[] calldata vouchers, bytes[] calldata signatures)
        external
        returns (uint256 total)
    {
        Channel storage ch = _channels[channelId];
        if (ch.payer == address(0)) revert UnknownChannel();
        if (ch.settled) revert ChannelSettled();
        uint256 n = vouchers.length;
        if (n == 0 || n != signatures.length) revert BadBatch();

        for (uint256 i = 0; i < n; ++i) {
            Voucher calldata v = vouchers[i];
            if (v.channelId != channelId) revert WrongChannel();
            if (v.validBefore <= block.timestamp) revert VoucherExpired(v.subject);
            if (_recover(voucherHash(v), signatures[i]) != ch.payer) revert BadSignature();

            uint256 already = _subjectRedeemed[channelId][v.subject];
            if (v.cumulative <= already) revert StaleVoucher(v.subject, v.cumulative, already);
            uint256 delta = v.cumulative - already;
            _subjectRedeemed[channelId][v.subject] = v.cumulative;
            if (ch.guarded) guard.consume(channelId, v.subject, delta);
            total += delta;
            emit VoucherRedeemed(channelId, v.subject, v.cumulative, delta);
        }

        uint256 outstanding = ch.deposit - ch.redeemed;
        if (total > outstanding) revert Underfunded(outstanding, total);
        ch.redeemed += total;

        emit Redeemed(channelId, ch.service, total, n);
        if (!usdc.transfer(ch.service, total)) revert TransferFailed();
    }

    // ---- close ------------------------------------------------------------

    /// @notice Payer asks to close. The service can still redeem until closeAt.
    function requestClose(bytes32 channelId) external returns (uint64 closeAt) {
        Channel storage ch = _channels[channelId];
        if (ch.payer == address(0)) revert UnknownChannel();
        if (msg.sender != ch.payer) revert NotPayer();
        if (ch.settled) revert ChannelSettled();
        if (ch.closeAt != 0) revert AlreadyClosing();
        closeAt = uint64(block.timestamp) + challengeWindow;
        ch.closeAt = closeAt;
        emit CloseRequested(channelId, closeAt);
    }

    /// @notice Payer takes the unspent remainder once the challenge window ends.
    function withdraw(bytes32 channelId) external returns (uint256 refund) {
        Channel storage ch = _channels[channelId];
        if (ch.payer == address(0)) revert UnknownChannel();
        if (msg.sender != ch.payer) revert NotPayer();
        if (ch.settled) revert ChannelSettled();
        if (ch.closeAt == 0) revert NotClosing();
        if (block.timestamp < ch.closeAt) revert ChallengeOpen(ch.closeAt);
        refund = _settle(ch, channelId, false);
    }

    /// @notice Close immediately with both sides' signatures, submitted by anyone.
    /// @dev Both sign the same Close(channelId, redeemed) digest, so the agreement
    ///      is pinned to what has already been paid out and a stale signature is
    ///      worthless once the service redeems more. Because anyone can submit it,
    ///      the payer's wallet stays free of transactions for the entire life of
    ///      the channel: it signs to open, signs every voucher, signs to close.
    function closeMutual(bytes32 channelId, bytes calldata payerSignature, bytes calldata serviceSignature)
        external
        returns (uint256 refund)
    {
        Channel storage ch = _channels[channelId];
        if (ch.payer == address(0)) revert UnknownChannel();
        if (ch.settled) revert ChannelSettled();
        bytes32 digest = closeHash(channelId, ch.redeemed);
        if (_recover(digest, payerSignature) != ch.payer) revert BadSignature();
        if (_recover(digest, serviceSignature) != ch.service) revert BadSignature();
        refund = _settle(ch, channelId, true);
    }

    function _settle(Channel storage ch, bytes32 channelId, bool cooperative) private returns (uint256 refund) {
        refund = ch.deposit - ch.redeemed;
        ch.settled = true;
        emit ChannelClosed(channelId, refund, cooperative);
        if (refund > 0 && !usdc.transfer(ch.payer, refund)) revert TransferFailed();
    }

    // ---- views and hashing ------------------------------------------------

    /// @notice Deterministic channel id, so both sides can derive it off-chain.
    function channelIdOf(address payer, address service, bytes32 salt) public pure returns (bytes32) {
        return keccak256(abi.encode(payer, service, salt));
    }

    function channelOf(bytes32 channelId) external view returns (Channel memory) {
        return _channels[channelId];
    }

    /// @notice What is left to spend in this channel.
    function outstanding(bytes32 channelId) external view returns (uint256) {
        Channel memory ch = _channels[channelId];
        if (ch.settled) return 0;
        return ch.deposit - ch.redeemed;
    }

    /// @notice Total already settled for one subject in this channel.
    function subjectRedeemed(bytes32 channelId, bytes32 subject) external view returns (uint256) {
        return _subjectRedeemed[channelId][subject];
    }

    /// @notice The EIP-712 digest a payer signs for a voucher.
    /// @dev Exposed so the off-chain signer can assert byte-for-byte agreement
    ///      with the contract instead of trusting two implementations to match.
    function voucherHash(Voucher calldata v) public view returns (bytes32) {
        return _digest(
            keccak256(abi.encode(VOUCHER_TYPEHASH, v.channelId, v.subject, v.cumulative, v.validBefore))
        );
    }

    /// @notice The EIP-712 digest a service signs to agree an immediate close.
    function closeHash(bytes32 channelId, uint256 redeemed) public view returns (bytes32) {
        return _digest(keccak256(abi.encode(CLOSE_TYPEHASH, channelId, redeemed)));
    }

    function domainSeparator() public view returns (bytes32) {
        return block.chainid == _cachedChainId ? _cachedDomainSeparator : _buildDomainSeparator();
    }

    function _digest(bytes32 structHash) private view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    function _buildDomainSeparator() private view returns (bytes32) {
        return keccak256(
            abi.encode(_EIP712_DOMAIN_TYPEHASH, _NAME_HASH, _VERSION_HASH, block.chainid, address(this))
        );
    }

    /// @dev ecrecover with the two checks a raw ecrecover skips: reject the
    ///      malleable high-s twin and reject a v outside {27, 28}.
    function _recover(bytes32 digest, bytes calldata signature) private pure returns (address signer) {
        if (signature.length != 65) revert BadSignature();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 0x20))
            v := byte(0, calldataload(add(signature.offset, 0x40)))
        }
        if (uint256(s) > _HALF_ORDER) revert BadSignature();
        if (v != 27 && v != 28) revert BadSignature();
        signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert BadSignature();
    }
}
