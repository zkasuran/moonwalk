// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {NanoChannel} from "../../src/NanoChannel.sol";
import {SpendGuard} from "../../src/SpendGuard.sol";
import {IUSDC} from "../../src/interfaces/IUSDC.sol";
import {MockUSDC} from "../mocks/MockUSDC.sol";

/// @title NanoChannelHandler
/// @notice Drives NanoChannel through random open/topUp/redeem/close sequences
///         with a small set of known-key actors. All signing happens here
///         because a channel only ever moves on the payer's signature, so the
///         handler holds the payer and service keys and builds every EIP-712
///         payload the way an off-chain client would.
/// @dev Actions return quietly on states the contract would reject on precondition
///      (settled, already closing, unknown), and let the contract revert on the
///      economic reverts (underfunded, cap exceeded, stale voucher). The invariant
///      profile runs with fail_on_revert = false, so those reverts are discarded
///      and the sequence continues.
contract NanoChannelHandler is Test {
    NanoChannel public channel;
    SpendGuard public guard;
    MockUSDC public usdc;

    uint256[] internal payerKeys;
    uint256[] internal serviceKeys;
    bytes32[] internal subjects;

    bytes32[] public channelIds;
    mapping(bytes32 => bool) internal known;

    struct Meta {
        uint256 payerKey;
        uint256 serviceKey;
        address payer;
        address service;
    }

    mapping(bytes32 => Meta) internal meta;

    // Ghost state the invariants read.
    mapping(bytes32 => uint256) public hwRedeemed;
    mapping(bytes32 => uint256) public hwDeposit;
    mapping(bytes32 => bool) public settledGhost;

    uint256 internal nonceCounter;

    constructor(NanoChannel channel_, SpendGuard guard_, MockUSDC usdc_) {
        channel = channel_;
        guard = guard_;
        usdc = usdc_;

        payerKeys.push(0xA11CE01);
        payerKeys.push(0xA11CE02);
        payerKeys.push(0xA11CE03);
        for (uint256 i = 0; i < payerKeys.length; ++i) {
            usdc.mint(vm.addr(payerKeys[i]), 1e30);
        }

        serviceKeys.push(0xB0B01);
        serviceKeys.push(0xB0B02);

        subjects.push(keccak256("discord:1:alice"));
        subjects.push(keccak256("discord:1:bob"));
        subjects.push(keccak256("discord:1:carol"));
    }

    // ---- getters the invariant contract reads ------------------------------

    function ids() external view returns (bytes32[] memory) {
        return channelIds;
    }

    function subjectsList() external view returns (bytes32[] memory) {
        return subjects;
    }

    // ---- helpers -----------------------------------------------------------

    function _auth(uint256 key, uint256 value)
        internal
        returns (NanoChannel.Authorization memory a)
    {
        a.from = vm.addr(key);
        a.value = value;
        a.validAfter = 0;
        a.validBefore = block.timestamp + 1 days;
        a.nonce = keccak256(abi.encode("nonce", nonceCounter++));
        bytes32 structHash = keccak256(
            abi.encode(
                usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(),
                a.from,
                address(channel),
                a.value,
                a.validAfter,
                a.validBefore,
                a.nonce
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        a.signature = abi.encodePacked(r, s, v);
    }

    function _sync(bytes32 id) internal {
        NanoChannel.Channel memory ch = channel.channelOf(id);
        if (ch.redeemed > hwRedeemed[id]) hwRedeemed[id] = ch.redeemed;
        if (ch.deposit > hwDeposit[id]) hwDeposit[id] = ch.deposit;
        if (ch.settled) settledGhost[id] = true;
    }

    function _sig(uint256 key, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    // ---- fuzzed actions ----------------------------------------------------

    function open(
        uint256 actorSeed,
        uint256 svcSeed,
        uint256 saltSeed,
        bool guarded,
        uint256 depositSeed,
        uint256 capSeed
    ) external {
        uint256 pk = payerKeys[actorSeed % payerKeys.length];
        uint256 sk = serviceKeys[svcSeed % serviceKeys.length];
        address service = vm.addr(sk);
        // A small salt space so the same (payer, service, salt) recurs and the
        // duplicate-open path is exercised alongside fresh channels.
        bytes32 salt = keccak256(abi.encode("salt", saltSeed % 8));
        bytes32 id = channel.channelIdOf(vm.addr(pk), service, salt);
        if (known[id] || channel.channelOf(id).payer != address(0)) return;

        _openChannel(pk, service, salt, guarded, bound(depositSeed, 1, 1e12), guarded ? bound(capSeed, 0, 1e12) : 0);

        channelIds.push(id);
        known[id] = true;
        meta[id] = Meta({payerKey: pk, serviceKey: sk, payer: vm.addr(pk), service: service});
        _sync(id);
    }

    /// Split out of open to keep the fuzz seeds off the stack while the 8-field
    /// Open payload is built (otherwise the frame goes stack-too-deep).
    function _openChannel(
        uint256 pk,
        address service,
        bytes32 salt,
        bool guarded,
        uint256 deposit,
        uint256 capLimit
    ) internal {
        NanoChannel.Authorization memory auth = _auth(pk, deposit);
        bytes memory openSig =
            _sig(pk, channel.openHash(service, salt, guarded, address(0), deposit, capLimit, 0, auth.nonce));
        channel.open(service, salt, guarded, address(0), capLimit, 0, auth, openSig);
    }

    function topUp(uint256 chSeed, uint256 amountSeed) external {
        if (channelIds.length == 0) return;
        bytes32 id = channelIds[chSeed % channelIds.length];
        if (channel.channelOf(id).settled) return;
        NanoChannel.Authorization memory auth = _auth(meta[id].payerKey, bound(amountSeed, 1, 1e12));
        channel.topUp(id, auth);
        _sync(id);
    }

    function redeem(uint256 chSeed, uint256 subjSeed, uint256 addSeed) external {
        if (channelIds.length == 0) return;
        bytes32 id = channelIds[chSeed % channelIds.length];
        if (channel.channelOf(id).settled) return;
        bytes32 subject = subjects[subjSeed % subjects.length];
        uint256 cumulative = channel.subjectRedeemed(id, subject) + bound(addSeed, 1, 1e9);
        NanoChannel.Voucher memory vch = NanoChannel.Voucher({
            channelId: id,
            subject: subject,
            cumulative: cumulative,
            validBefore: uint64(block.timestamp + 1 days)
        });
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(meta[id].payerKey, channel.voucherHash(vch));
        NanoChannel.Voucher[] memory vs = new NanoChannel.Voucher[](1);
        vs[0] = vch;
        bytes[] memory sigs = new bytes[](1);
        sigs[0] = abi.encodePacked(r, s, v);
        // May revert on Underfunded or CapExceeded, which the profile discards.
        channel.redeem(id, vs, sigs);
        _sync(id);
    }

    function requestClose(uint256 chSeed) external {
        if (channelIds.length == 0) return;
        bytes32 id = channelIds[chSeed % channelIds.length];
        NanoChannel.Channel memory ch = channel.channelOf(id);
        if (ch.settled || ch.closeAt != 0) return;
        vm.prank(meta[id].payer);
        channel.requestClose(id);
    }

    function withdraw(uint256 chSeed, uint256 warpSeed) external {
        if (channelIds.length == 0) return;
        bytes32 id = channelIds[chSeed % channelIds.length];
        NanoChannel.Channel memory ch = channel.channelOf(id);
        if (ch.settled || ch.closeAt == 0) return;
        uint256 target = uint256(ch.closeAt) + bound(warpSeed, 0, 7 days);
        if (block.timestamp < target) vm.warp(target);
        vm.prank(meta[id].payer);
        channel.withdraw(id);
        _sync(id);
    }

    function closeMutual(uint256 chSeed) external {
        if (channelIds.length == 0) return;
        bytes32 id = channelIds[chSeed % channelIds.length];
        NanoChannel.Channel memory ch = channel.channelOf(id);
        if (ch.settled) return;
        bytes32 digest = channel.closeHash(id, ch.redeemed);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(meta[id].payerKey, digest);
        (uint8 sv, bytes32 sr, bytes32 ss) = vm.sign(meta[id].serviceKey, digest);
        channel.closeMutual(id, abi.encodePacked(pr, ps, pv), abi.encodePacked(sr, ss, sv));
        _sync(id);
    }
}

/// @title NanoChannelInvariants
/// @notice The accounting properties that must hold after any sequence of
///         channel operations, checked against the handler's random driving.
contract NanoChannelInvariants is StdInvariant, Test {
    MockUSDC internal usdc;
    SpendGuard internal guard;
    NanoChannel internal channel;
    NanoChannelHandler internal handler;

    function setUp() public {
        vm.warp(1_700_000_000);
        usdc = new MockUSDC();
        guard = new SpendGuard();
        channel = new NanoChannel(IUSDC(address(usdc)), guard, 1 hours);
        handler = new NanoChannelHandler(channel, guard, usdc);

        bytes4[] memory sel = new bytes4[](6);
        sel[0] = NanoChannelHandler.open.selector;
        sel[1] = NanoChannelHandler.topUp.selector;
        sel[2] = NanoChannelHandler.redeem.selector;
        sel[3] = NanoChannelHandler.requestClose.selector;
        sel[4] = NanoChannelHandler.withdraw.selector;
        sel[5] = NanoChannelHandler.closeMutual.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sel}));
        targetContract(address(handler));
    }

    /// The contract holds exactly the sum of what every live channel has left to
    /// spend, no more (nobody's funds are strandable) and no less (nothing is
    /// double spent).
    function invariant_tokenConservation() public view {
        bytes32[] memory chIds = handler.ids();
        uint256 expected;
        for (uint256 i; i < chIds.length; ++i) {
            NanoChannel.Channel memory ch = channel.channelOf(chIds[i]);
            if (!ch.settled) expected += ch.deposit - ch.redeemed;
        }
        assertEq(usdc.balanceOf(address(channel)), expected, "balance equals live outstanding");
    }

    /// A channel's redeemed total is exactly the sum of its per-subject totals.
    function invariant_redeemedEqualsSubjectSum() public view {
        bytes32[] memory chIds = handler.ids();
        bytes32[] memory subs = handler.subjectsList();
        for (uint256 i; i < chIds.length; ++i) {
            uint256 sum;
            for (uint256 j; j < subs.length; ++j) {
                sum += channel.subjectRedeemed(chIds[i], subs[j]);
            }
            assertEq(channel.channelOf(chIds[i]).redeemed, sum, "redeemed equals subject total");
        }
    }

    function invariant_redeemedNeverExceedsDeposit() public view {
        bytes32[] memory chIds = handler.ids();
        for (uint256 i; i < chIds.length; ++i) {
            NanoChannel.Channel memory ch = channel.channelOf(chIds[i]);
            assertLe(ch.redeemed, ch.deposit, "never pay out more than funded");
        }
    }

    /// Deposit and redeemed only ever go up. The ghost high-water marks are set
    /// from on-chain state, so a decrease would drop below the recorded mark.
    function invariant_countersAreMonotonic() public view {
        bytes32[] memory chIds = handler.ids();
        for (uint256 i; i < chIds.length; ++i) {
            NanoChannel.Channel memory ch = channel.channelOf(chIds[i]);
            assertGe(ch.redeemed, handler.hwRedeemed(chIds[i]), "redeemed never decreases");
            assertGe(ch.deposit, handler.hwDeposit(chIds[i]), "deposit never decreases");
        }
    }

    /// Once a channel settles it stays settled.
    function invariant_settledIsTerminal() public view {
        bytes32[] memory chIds = handler.ids();
        for (uint256 i; i < chIds.length; ++i) {
            if (handler.settledGhost(chIds[i])) {
                assertTrue(channel.channelOf(chIds[i]).settled, "a settled channel never reopens");
            }
        }
    }

    /// For every guarded scope and subject, booked usage never exceeds the cap
    /// in force. consume enforces this at write time; this proves no path around
    /// it accumulates usage past the limit.
    function invariant_usageNeverExceedsCap() public view {
        bytes32[] memory chIds = handler.ids();
        bytes32[] memory subs = handler.subjectsList();
        for (uint256 i; i < chIds.length; ++i) {
            if (!channel.channelOf(chIds[i]).guarded) continue;
            for (uint256 j; j < subs.length; ++j) {
                (uint256 limit,, bool set) = guard.capOf(address(channel), chIds[i], subs[j]);
                (uint256 used,) = guard.usageOf(address(channel), chIds[i], subs[j]);
                if (set) assertLe(used, limit, "booked usage stays within the cap");
            }
        }
    }
}
