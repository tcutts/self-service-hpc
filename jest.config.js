module.exports = {
  projects: [
    {
      displayName: 'backend',
      testEnvironment: 'node',
      roots: ['<rootDir>/test'],
      testMatch: ['**/*.test.ts'],
      testPathIgnorePatterns: ['<rootDir>/test/frontend/'],
      transform: {
        '^.+\\.tsx?$': 'ts-jest',
      },
    },
    {
      displayName: 'frontend',
      testEnvironment: 'jsdom',
      roots: ['<rootDir>/test/frontend'],
      testMatch: ['**/*.test.js'],
    },
  ],
};
