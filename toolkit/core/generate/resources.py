from __future__ import annotations


def calculate_service_limits(vm_ram_mb: int, vm_cpus: int, services: list[str]) -> dict[str, dict[str, str]]:
    """Compute mem_limit + cpus per service, bucketed by memory_tier.

    Reads each service's tier from category.yaml via service_metadata.get_service_memory_tier.
    No hardcoded service lists are used. The minimums come from each service
    manifest's resource floors.
    """
    from toolkit.core.config.service_metadata import get_service_memory_tier, get_service_resource_requirements

    allocatable_ram = vm_ram_mb - 512
    allocatable_cpu = vm_cpus * 0.9
    tier1 = [s for s in services if get_service_memory_tier(s) == "heavy"]
    tier2 = [s for s in services if get_service_memory_tier(s) == "medium"]
    tier3 = [s for s in services if get_service_memory_tier(s) not in ("heavy", "medium")]
    result = {}
    for tier_services, budget in [(tier1, 0.50), (tier2, 0.30), (tier3, 0.20)]:
        if not tier_services:
            continue
        tier_ram = int(allocatable_ram * budget)
        tier_cpu = allocatable_cpu * budget
        per_ram = max(tier_ram // len(tier_services), 128)
        per_cpu = round(tier_cpu / len(tier_services), 2)
        for svc in tier_services:
            memory_floor_mb, cpu_floor = get_service_resource_requirements(svc)
            ram_mb = max(per_ram, memory_floor_mb)
            result[svc] = {"mem_limit": f"{ram_mb}m", "cpus": str(max(per_cpu, cpu_floor))}
    return result
