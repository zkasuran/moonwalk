// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, Vm} from "forge-std/Test.sol";
import {NanoChannel} from "../src/NanoChannel.sol";
import {SpendGuard} from "../src/SpendGuard.sol";
import {IUSDC} from "../src/interfaces/IUSDC.sol";
import {MockUSDC} from "./mocks/MockUSDC.sol";

contract NanoChannelTest is Test {
    MockUSDC usdc;
    SpendGuard guard;
    NanoChannel channel;

    uint256 constant PAYER_KEY = 0xA11CE;
    uint256 constant SERVICE_KEY = 0xB0B;
    uint256 constant STRANGER_KEY = 0xBAD;
    uint64 constant CHALLENGE = 1 hours;

    address payer;
    address service;
    address stranger;

    bytes32 constant ALICE = keccak256("discord:900:alice");
    bytes32 constant BOB = keccak256("discord:900:bob");
    bytes32 constant SALT = keccak256("moonwalk-test");

    uint256 constant SECP256K1N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    function setUp() public {
        payer = vm.addr(PAYER_KEY);
        service = vm.addr(SERVICE_KEY);
        stranger = vm.addr(STRANGER_KEY);

        usdc = new MockUSDC();
        guard = new SpendGuard();
        channel = new NanoChannel(IUSDC(address(usdc)), guard, CHALLENGE);

        usdc.mint(payer, 100_000_000); // 100 USDC at 6 decimals
        vm.warp(1_700_000_000);
    }

    // ---- helpers ----------------------------------------------------------

    function _auth(uint256 key, uint256 value, bytes32 nonce)
        internal
        view
        returns (NanoChannel.Authorization memory a)
    {
        a.from = vm.addr(key);
        a.value = value;
        a.validAfter = 0;
        a.validBefore = block.timestamp + 1 hours;
        a.nonce = nonce;

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

    function _open(uint256 deposit, bool guarded) internal returns (bytes32 id) {
        id = channel.open(
            service, SALT, guarded, address(0), _auth(PAYER_KEY, deposit, keccak256("open"))
        );
    }

    function _voucher(bytes32 id, bytes32 subject, uint256 cumulative)
        internal
        view
        returns (NanoChannel.Voucher memory v)
    {
        v = NanoChannel.Voucher({
            channelId: id,
            subject: subject,
            cumulative: cumulative,
            validBefore: uint64(block.timestamp + 1 days)
        });
    }

    function _sign(uint256 key, NanoChannel.Voucher memory v) internal view returns (bytes memory) {
        (uint8 sv, bytes32 r, bytes32 s) = vm.sign(key, channel.voucherHash(v));
        return abi.encodePacked(r, s, sv);
    }

    function _one(NanoChannel.Voucher memory v)
        internal
        pure
        returns (NanoChannel.Voucher[] memory vs)
    {
        vs = new NanoChannel.Voucher[](1);
        vs[0] = v;
    }

    function _sigs(bytes memory sig) internal pure returns (bytes[] memory out) {
        out = new bytes[](1);
        out[0] = sig;
    }

    function _redeem(bytes32 id, bytes32 subject, uint256 cumulative) internal returns (uint256) {
        NanoChannel.Voucher memory v = _voucher(id, subject, cumulative);
        return channel.redeem(id, _one(v), _sigs(_sign(PAYER_KEY, v)));
    }

    /// Signing and hashing both make external calls, so anything that runs under
    /// vm.expectRevert has to be built first. This returns a ready batch.
    function _batch(bytes32 id, bytes32 subject, uint256 cumulative, uint256 key)
        internal
        view
        returns (NanoChannel.Voucher[] memory vs, bytes[] memory sigs)
    {
        NanoChannel.Voucher memory v = _voucher(id, subject, cumulative);
        vs = _one(v);
        sigs = _sigs(_sign(key, v));
    }

    // ---- funding ----------------------------------------------------------

    function test_OpenPullsDepositWithNoApproveAndNoPayerTx() public {
        uint256 before = usdc.balanceOf(payer);
        // The service submits the transaction. The payer only ever signed.
        vm.prank(service);
        bytes32 id = _open(5_000_000, false);

        assertEq(usdc.balanceOf(address(channel)), 5_000_000, "channel holds the deposit");
        assertEq(usdc.balanceOf(payer), before - 5_000_000, "payer funded it");
        assertEq(usdc.allowance(payer, address(channel)), 0, "no approve was needed");
        assertEq(id, channel.channelIdOf(payer, service, SALT), "id is derivable off-chain");

        NanoChannel.Channel memory ch = channel.channelOf(id);
        assertEq(ch.payer, payer);
        assertEq(ch.service, service);
        assertEq(ch.deposit, 5_000_000);
        assertEq(ch.redeemed, 0);
        assertEq(channel.outstanding(id), 5_000_000);
    }

    function test_OpenRejectsDuplicateChannel() public {
        _open(1_000_000, false);
        NanoChannel.Authorization memory a = _auth(PAYER_KEY, 1_000_000, keccak256("open2"));
        vm.expectRevert(NanoChannel.ChannelExists.selector);
        channel.open(service, SALT, false, address(0), a);
    }

    function test_OpenRejectsZeroService() public {
        NanoChannel.Authorization memory a = _auth(PAYER_KEY, 1_000_000, keccak256("z"));
        vm.expectRevert(NanoChannel.ZeroService.selector);
        channel.open(address(0), SALT, false, address(0), a);
    }

    function test_OpenRejectsZeroDeposit() public {
        NanoChannel.Authorization memory a = _auth(PAYER_KEY, 0, keccak256("z"));
        vm.expectRevert(NanoChannel.ZeroDeposit.selector);
        channel.open(service, SALT, false, address(0), a);
    }

    /// A payer that never sends a transaction cannot set its own caps, so an ops
    /// wallet can hold that job. It can only restrict spend, never authorize it.
    function test_CapOwnerCanBeDelegatedAwayFromThePayer() public {
        bytes32 id = channel.open(
            service, SALT, true, stranger, _auth(PAYER_KEY, 100_000, keccak256("open"))
        );
        assertEq(guard.scopeOwner(address(channel), id), stranger);

        vm.prank(payer);
        vm.expectRevert(SpendGuard.NotScopeOwner.selector);
        guard.setDefaultCap(address(channel), id, 1_000, 0);

        vm.prank(stranger);
        guard.setDefaultCap(address(channel), id, 1_000, 0);
        assertEq(_redeem(id, ALICE, 1_000), 1_000);
    }

    function test_TopUpAddsToTheDeposit() public {
        bytes32 id = _open(1_000_000, false);
        channel.topUp(id, _auth(PAYER_KEY, 2_500_000, keccak256("top")));
        assertEq(channel.channelOf(id).deposit, 3_500_000);
        assertEq(usdc.balanceOf(address(channel)), 3_500_000);
    }

    function test_TopUpRejectsAnotherPayer() public {
        bytes32 id = _open(1_000_000, false);
        usdc.mint(stranger, 1_000_000);
        NanoChannel.Authorization memory a = _auth(STRANGER_KEY, 500_000, keccak256("top"));
        vm.expectRevert(NanoChannel.WrongPayer.selector);
        channel.topUp(id, a);
    }

    function test_TopUpRejectsUnknownChannel() public {
        NanoChannel.Authorization memory a = _auth(PAYER_KEY, 1, keccak256("top"));
        vm.expectRevert(NanoChannel.UnknownChannel.selector);
        channel.topUp(keccak256("nope"), a);
    }

    // ---- redeem -----------------------------------------------------------

    function test_RedeemPaysTheDeltaOnly() public {
        bytes32 id = _open(1_000_000, false);

        assertEq(_redeem(id, ALICE, 3_000), 3_000, "first voucher pays its full cumulative");
        assertEq(usdc.balanceOf(service), 3_000);

        assertEq(_redeem(id, ALICE, 7_500), 4_500, "second pays only what is new");
        assertEq(usdc.balanceOf(service), 7_500);
        assertEq(channel.subjectRedeemed(id, ALICE), 7_500);
        assertEq(channel.outstanding(id), 1_000_000 - 7_500);
    }

    /// Forty sub-cent calls across two people, one on-chain transfer. This is the
    /// whole reason the channel exists.
    function test_RedeemBatchSettlesManyNanopaymentsInOneTransfer() public {
        bytes32 id = _open(1_000_000, false);

        uint256 aliceTotal = 25 * 1_000; // 25 calls at $0.001
        uint256 bobTotal = 15 * 1_000; // 15 calls at $0.001

        NanoChannel.Voucher[] memory vs = new NanoChannel.Voucher[](2);
        bytes[] memory sigs = new bytes[](2);
        vs[0] = _voucher(id, ALICE, aliceTotal);
        vs[1] = _voucher(id, BOB, bobTotal);
        sigs[0] = _sign(PAYER_KEY, vs[0]);
        sigs[1] = _sign(PAYER_KEY, vs[1]);

        vm.recordLogs();
        uint256 total = channel.redeem(id, vs, sigs);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        assertEq(total, 40_000, "40 calls at $0.001");
        assertEq(usdc.balanceOf(service), 40_000);
        assertEq(channel.subjectRedeemed(id, ALICE), 25_000);
        assertEq(channel.subjectRedeemed(id, BOB), 15_000);

        bytes32 transferSig = keccak256("Transfer(address,address,uint256)");
        uint256 transfers = 0;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].topics.length != 0 && logs[i].topics[0] == transferSig) transfers++;
        }
        assertEq(transfers, 1, "one USDC transfer settles the whole batch");
    }

    function test_RedeemRejectsStaleVoucher() public {
        bytes32 id = _open(1_000_000, false);
        _redeem(id, ALICE, 5_000);
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 5_000, PAYER_KEY);
        vm.expectRevert(abi.encodeWithSelector(NanoChannel.StaleVoucher.selector, ALICE, 5_000, 5_000));
        channel.redeem(id, vs, sigs);
    }

    function test_RedeemRejectsSignatureFromAnyoneButThePayer() public {
        bytes32 id = _open(1_000_000, false);
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 1_000, STRANGER_KEY);
        vm.expectRevert(NanoChannel.BadSignature.selector);
        channel.redeem(id, vs, sigs);
    }

    function test_RedeemRejectsExpiredVoucher() public {
        bytes32 id = _open(1_000_000, false);
        NanoChannel.Voucher memory v = _voucher(id, ALICE, 1_000);
        bytes memory sig = _sign(PAYER_KEY, v);
        vm.warp(uint256(v.validBefore) + 1);
        vm.expectRevert(abi.encodeWithSelector(NanoChannel.VoucherExpired.selector, ALICE));
        channel.redeem(id, _one(v), _sigs(sig));
    }

    function test_RedeemRejectsVoucherForAnotherChannel() public {
        bytes32 id = _open(1_000_000, false);
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) =
            _batch(keccak256("other"), ALICE, 1_000, PAYER_KEY);
        vm.expectRevert(NanoChannel.WrongChannel.selector);
        channel.redeem(id, vs, sigs);
    }

    function test_RedeemCannotExceedTheDeposit() public {
        bytes32 id = _open(10_000, false);
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 10_001, PAYER_KEY);
        vm.expectRevert(abi.encodeWithSelector(NanoChannel.Underfunded.selector, 10_000, 10_001));
        channel.redeem(id, vs, sigs);
    }

    function test_RedeemRejectsMalleableSignature() public {
        bytes32 id = _open(1_000_000, false);
        NanoChannel.Voucher memory v = _voucher(id, ALICE, 1_000);
        (uint8 sv, bytes32 r, bytes32 s) = vm.sign(PAYER_KEY, channel.voucherHash(v));
        // The other valid (r, s, v) for the same signature. ecrecover accepts it,
        // this contract must not.
        bytes32 flipped = bytes32(SECP256K1N - uint256(s));
        uint8 flippedV = sv == 27 ? 28 : 27;
        vm.expectRevert(NanoChannel.BadSignature.selector);
        channel.redeem(id, _one(v), _sigs(abi.encodePacked(r, flipped, flippedV)));
    }

    function test_RedeemRejectsEmptyAndMismatchedBatch() public {
        bytes32 id = _open(1_000_000, false);
        NanoChannel.Voucher[] memory none = new NanoChannel.Voucher[](0);
        vm.expectRevert(NanoChannel.BadBatch.selector);
        channel.redeem(id, none, new bytes[](0));

        NanoChannel.Voucher memory v = _voucher(id, ALICE, 1_000);
        vm.expectRevert(NanoChannel.BadBatch.selector);
        channel.redeem(id, _one(v), new bytes[](2));
    }

    // ---- guarded channels -------------------------------------------------

    function test_GuardedChannelBlocksSpendOverTheOnChainCap() public {
        bytes32 id = _open(1_000_000, true);
        vm.prank(payer);
        guard.setDefaultCap(address(channel), id, 5_000, 0);

        assertEq(_redeem(id, ALICE, 5_000), 5_000, "up to the cap is fine");
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 5_001, PAYER_KEY);
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, ALICE, 5_000, 1, 5_000));
        channel.redeem(id, vs, sigs);
    }

    function test_GuardedChannelFailsClosedWhenNoCapIsConfigured() public {
        bytes32 id = _open(1_000_000, true);
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 1, PAYER_KEY);
        vm.expectRevert(
            abi.encodeWithSelector(SpendGuard.NotConfigured.selector, address(channel), id, ALICE)
        );
        channel.redeem(id, vs, sigs);
    }

    function test_GuardedChannelGivesEachSubjectItsOwnCap() public {
        bytes32 id = _open(1_000_000, true);
        vm.startPrank(payer);
        guard.setDefaultCap(address(channel), id, 2_000, 0);
        guard.setSubjectCap(address(channel), id, ALICE, 50_000, 0);
        vm.stopPrank();

        assertEq(_redeem(id, ALICE, 40_000), 40_000, "alice has her own ceiling");
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, BOB, 2_001, PAYER_KEY);
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, BOB, 0, 2_001, 2_000));
        channel.redeem(id, vs, sigs);
        assertEq(_redeem(id, BOB, 2_000), 2_000, "bob is capped at the default");
        assertEq(guard.remaining(address(channel), id, ALICE), 10_000);
    }

    function test_ScopeOwnerIsThePayerNotTheCaller() public {
        bytes32 id = _open(1_000_000, true);
        assertEq(guard.scopeOwner(address(channel), id), payer);
        vm.prank(stranger);
        vm.expectRevert(SpendGuard.NotScopeOwner.selector);
        guard.setDefaultCap(address(channel), id, 1_000_000, 0);
    }

    // ---- close ------------------------------------------------------------

    function test_PayerWithdrawsTheRemainderAfterTheChallengeWindow() public {
        bytes32 id = _open(100_000, false);
        _redeem(id, ALICE, 30_000);

        vm.prank(payer);
        uint64 closeAt = channel.requestClose(id);
        assertEq(closeAt, uint64(block.timestamp) + CHALLENGE);

        vm.prank(payer);
        vm.expectRevert(abi.encodeWithSelector(NanoChannel.ChallengeOpen.selector, closeAt));
        channel.withdraw(id);

        // The service still gets to redeem while the window is open.
        assertEq(_redeem(id, BOB, 10_000), 10_000);

        vm.warp(uint256(closeAt));
        uint256 before = usdc.balanceOf(payer);
        vm.prank(payer);
        assertEq(channel.withdraw(id), 60_000, "deposit minus everything redeemed");
        assertEq(usdc.balanceOf(payer), before + 60_000);
        assertEq(usdc.balanceOf(address(channel)), 0);
        assertEq(channel.outstanding(id), 0);
    }

    function test_WithdrawNeedsACloseRequestAndOnlyThePayer() public {
        bytes32 id = _open(100_000, false);
        vm.prank(payer);
        vm.expectRevert(NanoChannel.NotClosing.selector);
        channel.withdraw(id);

        vm.prank(stranger);
        vm.expectRevert(NanoChannel.NotPayer.selector);
        channel.requestClose(id);
    }

    function test_RedeemStopsOnceTheChannelIsSettled() public {
        bytes32 id = _open(100_000, false);
        vm.startPrank(payer);
        uint64 closeAt = channel.requestClose(id);
        vm.warp(uint256(closeAt));
        channel.withdraw(id);
        vm.stopPrank();
        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, 1_000, PAYER_KEY);
        vm.expectRevert(NanoChannel.ChannelSettled.selector);
        channel.redeem(id, vs, sigs);
    }

    function test_MutualCloseSettlesWithoutASingleTransactionFromThePayer() public {
        bytes32 id = _open(100_000, false);
        _redeem(id, ALICE, 25_000);

        bytes32 digest = channel.closeHash(id, 25_000);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(PAYER_KEY, digest);
        (uint8 sv, bytes32 sr, bytes32 ss) = vm.sign(SERVICE_KEY, digest);

        uint256 before = usdc.balanceOf(payer);
        // A stranger submits it. Both sides only ever signed.
        vm.prank(stranger);
        uint256 refund =
            channel.closeMutual(id, abi.encodePacked(pr, ps, pv), abi.encodePacked(sr, ss, sv));
        assertEq(refund, 75_000);
        assertEq(usdc.balanceOf(payer), before + 75_000);
        assertEq(vm.getNonce(payer), 0, "the payer never sent a transaction");
    }

    function test_MutualCloseRejectsAStrangerStandingInForEitherSide() public {
        bytes32 id = _open(100_000, false);
        bytes32 digest = channel.closeHash(id, 0);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(PAYER_KEY, digest);
        (uint8 xv, bytes32 xr, bytes32 xs) = vm.sign(STRANGER_KEY, digest);
        bytes memory payerSig = abi.encodePacked(pr, ps, pv);
        bytes memory strangerSig = abi.encodePacked(xr, xs, xv);

        vm.expectRevert(NanoChannel.BadSignature.selector);
        channel.closeMutual(id, payerSig, strangerSig);

        vm.expectRevert(NanoChannel.BadSignature.selector);
        channel.closeMutual(id, strangerSig, payerSig);
    }

    function test_MutualCloseSignatureCannotBeReusedAfterMoreSpend() public {
        bytes32 id = _open(100_000, false);
        _redeem(id, ALICE, 25_000);
        bytes32 digest = channel.closeHash(id, 25_000);
        (uint8 pv, bytes32 pr, bytes32 ps) = vm.sign(PAYER_KEY, digest);
        (uint8 sv, bytes32 sr, bytes32 ss) = vm.sign(SERVICE_KEY, digest);
        bytes memory stalePayer = abi.encodePacked(pr, ps, pv);
        bytes memory staleService = abi.encodePacked(sr, ss, sv);

        // The service redeems again, so the agreement they both signed is stale.
        _redeem(id, BOB, 5_000);
        vm.expectRevert(NanoChannel.BadSignature.selector);
        channel.closeMutual(id, stalePayer, staleService);
    }

    // ---- EIP-712 ----------------------------------------------------------

    function test_DomainSeparatorMatchesTheSpec() public view {
        bytes32 expected = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("MoonWalk NanoChannel"),
                keccak256("1"),
                block.chainid,
                address(channel)
            )
        );
        assertEq(channel.domainSeparator(), expected);
    }

    function test_VoucherHashMatchesTheSpec() public view {
        NanoChannel.Voucher memory v = NanoChannel.Voucher({
            channelId: keccak256("c"),
            subject: ALICE,
            cumulative: 1_234,
            validBefore: 99_999
        });
        bytes32 structHash = keccak256(
            abi.encode(
                keccak256("Voucher(bytes32 channelId,bytes32 subject,uint256 cumulative,uint64 validBefore)"),
                v.channelId,
                v.subject,
                v.cumulative,
                v.validBefore
            )
        );
        bytes32 expected = keccak256(abi.encodePacked("\x19\x01", channel.domainSeparator(), structHash));
        assertEq(channel.voucherHash(v), expected);
    }

    // ---- fuzz -------------------------------------------------------------

    /// Whatever order vouchers arrive in, the service can never collect more than
    /// the highest cumulative the payer signed.
    function testFuzz_RedeemNeverPaysMoreThanTheHighestCumulative(uint32 a, uint32 b) public {
        uint256 first = uint256(a) + 1;
        uint256 second = uint256(b) + 1;
        vm.assume(first != second);
        uint256 high = first > second ? first : second;
        uint256 low = first > second ? second : first;

        usdc.mint(payer, 10_000_000_000);
        bytes32 id = _open(10_000_000_000, false);
        _redeem(id, ALICE, low);
        _redeem(id, ALICE, high);
        assertEq(usdc.balanceOf(service), high, "total paid equals the top cumulative");

        (NanoChannel.Voucher[] memory vs, bytes[] memory sigs) = _batch(id, ALICE, low, PAYER_KEY);
        vm.expectRevert(abi.encodeWithSelector(NanoChannel.StaleVoucher.selector, ALICE, low, high));
        channel.redeem(id, vs, sigs);
    }
}
