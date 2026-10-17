// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @title ReceiptRegistry
/// @notice Timestamps hashes of enclave-signed processing receipts. A hash reveals nothing
///         about the tenant or the document; only the holder of the receipt can link them.
contract ReceiptRegistry {
    address public owner;
    mapping(address relayer => bool allowed) public isRelayer;
    mapping(bytes32 receiptHash => uint256 timestamp) public anchoredAt;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event RelayerSet(address indexed relayer, bool allowed);
    event ReceiptAnchored(bytes32 indexed receiptHash, uint256 timestamp);

    error NotOwner();
    error NotRelayer();
    error ZeroAddress();
    error ZeroHash();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address relayer) {
        _transferOwnership(msg.sender);
        _setRelayer(relayer, true);
    }

    /// @notice Records `receiptHash` once. Anchoring it again is a no-op that returns the
    ///         original timestamp, so a relayer can safely retry a transaction it lost track of.
    function anchor(bytes32 receiptHash) external returns (uint256 timestamp) {
        if (!isRelayer[msg.sender]) revert NotRelayer();
        if (receiptHash == bytes32(0)) revert ZeroHash();
        timestamp = anchoredAt[receiptHash];
        if (timestamp != 0) return timestamp;
        timestamp = block.timestamp;
        anchoredAt[receiptHash] = timestamp;
        emit ReceiptAnchored(receiptHash, timestamp);
    }

    function setRelayer(address relayer, bool allowed) external onlyOwner {
        _setRelayer(relayer, allowed);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        _transferOwnership(newOwner);
    }

    function _setRelayer(address relayer, bool allowed) private {
        if (relayer == address(0)) revert ZeroAddress();
        isRelayer[relayer] = allowed;
        emit RelayerSet(relayer, allowed);
    }

    function _transferOwnership(address newOwner) private {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
}
