using System;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Microsoft.VisualStudio.TestTools.UnitTesting;

internal static class CollectionSchedulingContract
{
    [ModuleInitializer]
    internal static void Verify()
    {
        var assembly = typeof(CollectionSchedulingContract).Assembly;
        if (assembly.IsDefined(typeof(DoNotParallelizeAttribute)) ||
            assembly.GetCustomAttribute<ParallelizeAttribute>() is null)
        {
            return;
        }

        var members = new[] { typeof(FirstMember), typeof(SecondMember) };
        var methods = new[] { nameof(FirstMember.FirstCase), nameof(SecondMember.SecondCase) };
        var policies = members.Select((member, index) =>
            member.GetCustomAttributes(inherit: true)
                .Concat(member.GetMethod(methods[index])!.GetCustomAttributes(inherit: true))
                .ToArray()).ToArray();

        // One deferred member cannot overlap the other in MSTest's parallel phase.
        if (policies.Any(attributes => attributes.Any(a => a is DoNotParallelizeAttribute)))
        {
            return;
        }

        var resources = policies.Select(attributes => attributes
            .Where(a => a.GetType().FullName ==
                "Microsoft.VisualStudio.TestTools.UnitTesting.ResourceLockAttribute")
            .Select(a => new
            {
                Name = (string)a.GetType().GetProperty("Resource")!.GetValue(a)!,
                Exclusive = a.GetType().GetProperty("Mode")!.GetValue(a)!.ToString() == "ReadWrite"
            }).ToArray()).ToArray();
        if (resources[0].Any(first => resources[1].Any(second =>
            first.Name == second.Name && (first.Exclusive || second.Exclusive))))
        {
            return;
        }

        throw new InvalidOperationException(
            "Collection scheduling contract: shared members can overlap.");
    }
}
