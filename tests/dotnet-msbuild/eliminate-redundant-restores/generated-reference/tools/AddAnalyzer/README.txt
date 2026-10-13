AddAnalyzer: a repo tool run by CI before the build.

  dotnet run --project tools/AddAnalyzer -- <project.csproj>

It edits <project.csproj> in place and adds
  <PackageReference Include="Contoso.Analyzers" Version="3.2.0" PrivateAssets="all" />
when the project does not already reference it. It does not run restore.
