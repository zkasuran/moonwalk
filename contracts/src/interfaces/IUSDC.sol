// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IUSDC
/// @notice The slice of Arc USDC that MoonWalk uses: the ERC-20 basics plus the
///         EIP-3009 authorization calls.
/// @dev Verified live against Arc testnet USDC at
///      0x3600000000000000000000000000000000000000 (name "USDC", version "2",
///      decimals 6). The proxy at that address forwards to implementation
///      0xc6ad664ac6679f4ce74e10e91449c93ec1ae3ca6, whose bytecode contains the
///      selectors for both the bytes and the (v, r, s) forms of
///      transferWithAuthorization and receiveWithAuthorization.
///
///      receiveWithAuthorization is the one that matters here. EIP-3009 requires
///      msg.sender == to for that call, which is exactly what lets a contract
///      pull a signed deposit: the payer signs off-chain and never sends a
///      transaction, and whoever submits the call pays the gas.
///
///      Arc uses USDC as its gas token with two views of one balance: the native
///      interface is 18 decimals, the ERC-20 interface is 6. Every amount in
///      MoonWalk is the 6 decimal ERC-20 view.
interface IUSDC {
    function transfer(address to, uint256 value) external returns (bool);

    function balanceOf(address account) external view returns (uint256);

    function decimals() external view returns (uint8);

    /// @notice Pull `value` from `from` on the strength of an off-chain signature.
    /// @dev Reverts unless msg.sender == to, so only the recipient contract can
    ///      redeem the authorization. Nonce is a random bytes32 chosen by the
    ///      signer, not a sequential counter.
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes calldata signature
    ) external;

    /// @notice True once an authorization nonce has been used or cancelled.
    function authorizationState(address authorizer, bytes32 nonce) external view returns (bool);
}
