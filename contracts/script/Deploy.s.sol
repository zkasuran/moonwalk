// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {NanoChannel} from "../src/NanoChannel.sol";
import {ServiceRegistry} from "../src/ServiceRegistry.sol";
import {SpendGuard} from "../src/SpendGuard.sol";
import {IUSDC} from "../src/interfaces/IUSDC.sol";

/// @notice Deploys the MoonWalk contract set.
/// @dev Reads DEPLOYER_PRIVATE_KEY, and optionally ARC_USDC and CHALLENGE_WINDOW.
///      Prints one KEY=value line per address so the caller can capture the
///      deployment record without parsing broadcast artifacts.
///
///      forge script script/Deploy.s.sol:Deploy \
///        --rpc-url $ARC_RPC_URL --broadcast --slow -vvv
contract Deploy is Script {
    function run() external {
        uint256 pk = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address usdc = vm.envOr("ARC_USDC", 0x3600000000000000000000000000000000000000);
        uint64 challengeWindow = uint64(vm.envOr("CHALLENGE_WINDOW", uint256(1 hours)));

        vm.startBroadcast(pk);
        SpendGuard guard = new SpendGuard();
        NanoChannel channel = new NanoChannel(IUSDC(usdc), guard, challengeWindow);
        ServiceRegistry registry = new ServiceRegistry();
        vm.stopBroadcast();

        console2.log("CHAIN_ID=%s", block.chainid);
        console2.log("USDC=%s", usdc);
        console2.log("SPEND_GUARD=%s", address(guard));
        console2.log("NANO_CHANNEL=%s", address(channel));
        console2.log("SERVICE_REGISTRY=%s", address(registry));
        console2.log("CHALLENGE_WINDOW=%s", challengeWindow);
        console2.log("DEPLOYER=%s", vm.addr(pk));
    }
}
