// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {ReceiptRegistry} from "../src/ReceiptRegistry.sol";

contract ReceiptRegistryTest is Test {
    event RelayerSet(address indexed relayer, bool allowed);
    event ReceiptAnchored(bytes32 indexed receiptHash, uint256 timestamp);

    ReceiptRegistry internal registry;
    address internal owner = makeAddr("owner");
    address internal relayer = makeAddr("relayer");
    bytes32 internal constant RECEIPT = keccak256("receipt");

    function setUp() public {
        vm.prank(owner);
        registry = new ReceiptRegistry(relayer);
    }

    function test_DeployerOwnsAndConstructorRelayerIsAuthorised() public view {
        assertEq(registry.owner(), owner);
        assertTrue(registry.isRelayer(relayer));
    }

    function test_RelayerAnchorsAReceiptAtTheBlockTime() public {
        vm.warp(1_750_000_000);
        vm.expectEmit(address(registry));
        emit ReceiptAnchored(RECEIPT, 1_750_000_000);
        vm.prank(relayer);
        assertEq(registry.anchor(RECEIPT), 1_750_000_000);
        assertEq(registry.anchoredAt(RECEIPT), 1_750_000_000);
    }

    function test_AnchoringTwiceKeepsTheFirstTimestampAndEmitsOnce() public {
        vm.warp(1000);
        vm.prank(relayer);
        registry.anchor(RECEIPT);

        vm.warp(2000);
        vm.recordLogs();
        vm.prank(relayer);
        assertEq(registry.anchor(RECEIPT), 1000);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(registry.anchoredAt(RECEIPT), 1000);
    }

    function testFuzz_OnlyRelayersCanAnchor(address caller, bytes32 receiptHash) public {
        vm.assume(caller != relayer);
        vm.prank(caller);
        vm.expectRevert(ReceiptRegistry.NotRelayer.selector);
        registry.anchor(receiptHash);
    }

    function test_ZeroHashIsRejected() public {
        vm.prank(relayer);
        vm.expectRevert(ReceiptRegistry.ZeroHash.selector);
        registry.anchor(bytes32(0));
    }

    function test_OwnerCanAddAndRemoveRelayers() public {
        address second = makeAddr("second");
        vm.expectEmit(address(registry));
        emit RelayerSet(second, true);
        vm.prank(owner);
        registry.setRelayer(second, true);

        vm.prank(second);
        registry.anchor(RECEIPT);

        vm.prank(owner);
        registry.setRelayer(second, false);
        vm.prank(second);
        vm.expectRevert(ReceiptRegistry.NotRelayer.selector);
        registry.anchor(keccak256("other"));
    }

    function testFuzz_OnlyOwnerManagesRelayers(address caller) public {
        vm.assume(caller != owner);
        vm.startPrank(caller);
        vm.expectRevert(ReceiptRegistry.NotOwner.selector);
        registry.setRelayer(caller, true);
        vm.expectRevert(ReceiptRegistry.NotOwner.selector);
        registry.transferOwnership(caller);
        vm.stopPrank();
    }

    function test_OwnershipTransferMovesControl() public {
        address next = makeAddr("next");
        vm.prank(owner);
        registry.transferOwnership(next);
        assertEq(registry.owner(), next);

        vm.prank(owner);
        vm.expectRevert(ReceiptRegistry.NotOwner.selector);
        registry.setRelayer(owner, true);
    }

    function test_ZeroAddressesAreRejected() public {
        vm.expectRevert(ReceiptRegistry.ZeroAddress.selector);
        new ReceiptRegistry(address(0));

        vm.startPrank(owner);
        vm.expectRevert(ReceiptRegistry.ZeroAddress.selector);
        registry.setRelayer(address(0), true);
        vm.expectRevert(ReceiptRegistry.ZeroAddress.selector);
        registry.transferOwnership(address(0));
        vm.stopPrank();
    }
}
