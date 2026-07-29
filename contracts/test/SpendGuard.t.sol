// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {SpendGuard} from "../src/SpendGuard.sol";

/// Exercises the guard on its own, including the parts NanoChannel never reaches:
/// window rollover, lifetime caps and isolation between two calling apps.
contract SpendGuardTest is Test {
    SpendGuard guard;

    address owner = address(0xA11);
    address other = address(0xB22);
    bytes32 constant SCOPE = keccak256("scope-1");
    bytes32 constant ALICE = keccak256("discord:900:alice");
    bytes32 constant BOB = keccak256("discord:900:bob");

    function setUp() public {
        guard = new SpendGuard();
        vm.warp(1_700_000_000);
        guard.registerScope(SCOPE, owner); // this test contract is the "app"
    }

    function test_ScopeIsClaimedOnceAndOwnedByTheNamedOwner() public {
        assertEq(guard.scopeOwner(address(this), SCOPE), owner);
        vm.expectRevert(SpendGuard.ScopeTaken.selector);
        guard.registerScope(SCOPE, other);
    }

    function test_RegisterScopeRejectsZeroOwner() public {
        vm.expectRevert(SpendGuard.ZeroOwner.selector);
        guard.registerScope(keccak256("another"), address(0));
    }

    function test_OnlyTheScopeOwnerSetsCaps() public {
        vm.prank(other);
        vm.expectRevert(SpendGuard.NotScopeOwner.selector);
        guard.setDefaultCap(address(this), SCOPE, 100, 0);

        vm.prank(other);
        vm.expectRevert(SpendGuard.NotScopeOwner.selector);
        guard.setSubjectCap(address(this), SCOPE, ALICE, 100, 0);
    }

    function test_UnconfiguredScopeSpendsNothing() public {
        vm.expectRevert(
            abi.encodeWithSelector(SpendGuard.NotConfigured.selector, address(this), SCOPE, ALICE)
        );
        guard.consume(SCOPE, ALICE, 1);
        assertEq(guard.remaining(address(this), SCOPE, ALICE), 0);
    }

    function test_ExplicitZeroCapBlocksSpendButIsConfigured() public {
        vm.prank(owner);
        guard.setDefaultCap(address(this), SCOPE, 0, 0);
        (, , bool set) = guard.capOf(address(this), SCOPE, ALICE);
        assertTrue(set, "a zero limit is a decision, not an absence");
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, ALICE, 0, 1, 0));
        guard.consume(SCOPE, ALICE, 1);
    }

    function test_LifetimeCapNeverRolls() public {
        vm.prank(owner);
        guard.setDefaultCap(address(this), SCOPE, 1_000, 0);
        guard.consume(SCOPE, ALICE, 1_000);
        vm.warp(block.timestamp + 365 days);
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, ALICE, 1_000, 1, 1_000));
        guard.consume(SCOPE, ALICE, 1);
    }

    function test_WindowedCapRollsOverOnceTheWindowPasses() public {
        vm.prank(owner);
        guard.setDefaultCap(address(this), SCOPE, 500, 1 days);
        guard.consume(SCOPE, ALICE, 500);
        assertEq(guard.remaining(address(this), SCOPE, ALICE), 0);

        vm.warp(block.timestamp + 1 days - 1);
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, ALICE, 500, 1, 500));
        guard.consume(SCOPE, ALICE, 1);

        vm.warp(block.timestamp + 1);
        assertEq(guard.remaining(address(this), SCOPE, ALICE), 500, "fresh window");
        guard.consume(SCOPE, ALICE, 400);
        (uint256 used,) = guard.usageOf(address(this), SCOPE, ALICE);
        assertEq(used, 400, "usage restarted, it did not accumulate");
    }

    function test_SubjectCapOverridesTheDefaultBothWays() public {
        vm.startPrank(owner);
        guard.setDefaultCap(address(this), SCOPE, 1_000, 0);
        guard.setSubjectCap(address(this), SCOPE, ALICE, 10_000, 0);
        guard.setSubjectCap(address(this), SCOPE, BOB, 10, 0);
        vm.stopPrank();

        guard.consume(SCOPE, ALICE, 9_000);
        guard.consume(SCOPE, BOB, 10);
        vm.expectRevert(abi.encodeWithSelector(SpendGuard.CapExceeded.selector, BOB, 10, 1, 10));
        guard.consume(SCOPE, BOB, 1);
        assertEq(guard.remaining(address(this), SCOPE, ALICE), 1_000);
    }

    /// Two apps can use the same scope bytes without ever touching each other's
    /// usage, because the scope key is namespaced by the calling contract.
    function test_ScopesAreNamespacedByTheCallingApp() public {
        SpendGuardCaller app = new SpendGuardCaller(guard);
        app.register(SCOPE, owner);
        vm.startPrank(owner);
        guard.setDefaultCap(address(this), SCOPE, 100, 0);
        guard.setDefaultCap(address(app), SCOPE, 100, 0);
        vm.stopPrank();

        guard.consume(SCOPE, ALICE, 100);
        app.consume(SCOPE, ALICE, 100); // untouched by the other app's usage

        (uint256 mine,) = guard.usageOf(address(this), SCOPE, ALICE);
        (uint256 theirs,) = guard.usageOf(address(app), SCOPE, ALICE);
        assertEq(mine, 100);
        assertEq(theirs, 100);
    }

    function testFuzz_ConsumeNeverExceedsTheCap(uint96 limit, uint96 first, uint96 second) public {
        vm.prank(owner);
        guard.setDefaultCap(address(this), SCOPE, limit, 0);
        vm.assume(uint256(first) + uint256(second) > uint256(limit));
        if (first > limit) {
            vm.expectRevert();
            guard.consume(SCOPE, ALICE, first);
            return;
        }
        guard.consume(SCOPE, ALICE, first);
        vm.expectRevert();
        guard.consume(SCOPE, ALICE, second);
        (uint256 used,) = guard.usageOf(address(this), SCOPE, ALICE);
        assertLe(used, limit);
    }
}

/// A second "app" so the isolation test has two distinct callers.
contract SpendGuardCaller {
    SpendGuard immutable guard;

    constructor(SpendGuard guard_) {
        guard = guard_;
    }

    function register(bytes32 scope, address owner) external {
        guard.registerScope(scope, owner);
    }

    function consume(bytes32 scope, bytes32 subject, uint256 amount) external {
        guard.consume(scope, subject, amount);
    }
}
