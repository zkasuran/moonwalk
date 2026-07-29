// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ServiceRegistry} from "../src/ServiceRegistry.sol";

contract ServiceRegistryTest is Test {
    ServiceRegistry registry;

    address admin = address(0xA0);
    address lister = address(0xB0);
    address outsider = address(0xC0);
    address payTo = address(0xD0);
    address usdc = 0x3600000000000000000000000000000000000000;

    bytes32 constant NS = keccak256("discord:900");
    string constant NAME = "sports_odds";
    string constant ENDPOINT = "https://odds.example.com/api";

    function setUp() public {
        registry = new ServiceRegistry();
        vm.prank(admin);
        registry.claimNamespace(NS);
    }

    function _register(uint256 price) internal returns (bytes32 id) {
        vm.prank(lister);
        id = registry.register(NS, NAME, "live odds", ENDPOINT, payTo, usdc, price);
    }

    function test_NamespaceIsClaimedOnce() public {
        assertEq(registry.namespaceAdmin(NS), admin);
        vm.prank(outsider);
        vm.expectRevert(ServiceRegistry.NamespaceTaken.selector);
        registry.claimNamespace(NS);
    }

    function test_ListingIsInertUntilTheAdminVerifiesIt() public {
        bytes32 id = _register(1_000);
        assertFalse(registry.isBuyable(id), "unverified listings are not buyable");

        vm.prank(outsider);
        vm.expectRevert(ServiceRegistry.NotNamespaceAdmin.selector);
        registry.setVerified(id, true);

        vm.prank(admin);
        registry.setVerified(id, true);
        assertTrue(registry.isBuyable(id));
    }

    function test_APriceChangeDropsTheVerification() public {
        bytes32 id = _register(1_000);
        vm.prank(admin);
        registry.setVerified(id, true);

        vm.prank(lister);
        registry.setPrice(id, 9_000, payTo);
        assertFalse(registry.isBuyable(id), "an admin only ever approves a price they saw");
        assertEq(registry.getService(id).priceAtomic, 9_000);
    }

    function test_OnlyTheListerRepricesAndOnlyListerOrAdminDisables() public {
        bytes32 id = _register(1_000);
        vm.prank(outsider);
        vm.expectRevert(ServiceRegistry.NotLister.selector);
        registry.setPrice(id, 2_000, payTo);

        vm.prank(outsider);
        vm.expectRevert(ServiceRegistry.NotLister.selector);
        registry.setEnabled(id, false);

        vm.prank(admin);
        registry.setEnabled(id, false);
        assertFalse(registry.getService(id).enabled);

        vm.prank(lister);
        registry.setEnabled(id, true);
        assertTrue(registry.getService(id).enabled);
    }

    function test_NamespaceMaxPriceCapsListings() public {
        vm.prank(admin);
        registry.setMaxPrice(NS, 5_000);

        vm.prank(lister);
        vm.expectRevert(
            abi.encodeWithSelector(ServiceRegistry.PriceAboveNamespaceMax.selector, 6_000, 5_000)
        );
        registry.register(NS, NAME, "live odds", ENDPOINT, payTo, usdc, 6_000);

        bytes32 id = _register(5_000);
        vm.prank(lister);
        vm.expectRevert(
            abi.encodeWithSelector(ServiceRegistry.PriceAboveNamespaceMax.selector, 5_001, 5_000)
        );
        registry.setPrice(id, 5_001, payTo);
    }

    function test_RegisterValidatesItsInputs() public {
        vm.startPrank(lister);
        vm.expectRevert(ServiceRegistry.ZeroPrice.selector);
        registry.register(NS, NAME, "", ENDPOINT, payTo, usdc, 0);

        vm.expectRevert(ServiceRegistry.ZeroAddressField.selector);
        registry.register(NS, NAME, "", ENDPOINT, address(0), usdc, 1);

        vm.expectRevert(ServiceRegistry.NameLength.selector);
        registry.register(NS, "ab", "", ENDPOINT, payTo, usdc, 1);

        vm.expectRevert(ServiceRegistry.EndpointNotHttps.selector);
        registry.register(NS, NAME, "", "http://odds.example.com", payTo, usdc, 1);

        vm.expectRevert(ServiceRegistry.EndpointLength.selector);
        registry.register(NS, NAME, "", "https://a", payTo, usdc, 1);
        vm.stopPrank();
    }

    function test_DuplicateNameInTheSameNamespaceIsRejected() public {
        _register(1_000);
        vm.prank(lister);
        vm.expectRevert(ServiceRegistry.ServiceExists.selector);
        registry.register(NS, NAME, "again", ENDPOINT, payTo, usdc, 2_000);
    }

    function test_SameNameLivesFreelyInAnotherNamespace() public {
        bytes32 id1 = _register(1_000);
        bytes32 other = keccak256("discord:901");
        vm.prank(lister);
        bytes32 id2 = registry.register(other, NAME, "same name", ENDPOINT, payTo, usdc, 1_000);
        assertTrue(id1 != id2);
        assertEq(registry.countOf(NS), 1);
        assertEq(registry.countOf(other), 1);
    }

    function test_CatalogIsEnumerableAndIdsAreDerivable() public {
        bytes32 id = _register(1_000);
        assertEq(id, registry.serviceIdOf(NS, NAME));
        bytes32[] memory ids = registry.idsOf(NS);
        assertEq(ids.length, 1);
        assertEq(ids[0], id);
    }

    function test_NamespaceTransferMovesAdminRights() public {
        vm.prank(admin);
        registry.transferNamespace(NS, outsider);
        assertEq(registry.namespaceAdmin(NS), outsider);

        bytes32 id = _register(1_000);
        vm.prank(admin);
        vm.expectRevert(ServiceRegistry.NotNamespaceAdmin.selector);
        registry.setVerified(id, true);
    }

    function test_UnknownServiceReverts() public {
        vm.expectRevert(ServiceRegistry.UnknownService.selector);
        registry.getService(keccak256("nope"));
        assertFalse(registry.isBuyable(keccak256("nope")));
    }
}
