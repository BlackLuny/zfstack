   6edf0:	push   %rbp
   6edf1:	push   %r15
   6edf3:	push   %r14
   6edf5:	push   %r13
   6edf7:	push   %r12
   6edf9:	push   %rbx
   6edfa:	sub    $0x98,%rsp
   6ee01:	mov    %rsi,%rbx
   6ee04:	cmpb   $0x3,0xf3(%rdi)
   6ee0b:	je     6ee27 <zfstack::buf::BlockPool::put+0x37>
   6ee0d:	mov    0xb0(%rdi),%rax
   6ee14:	cmp    0x30(%rbx),%rax
   6ee18:	jne    6ee27 <zfstack::buf::BlockPool::put+0x37>
   6ee1a:	mov    0xb8(%rdi),%rax
   6ee21:	cmp    0x38(%rbx),%rax
   6ee25:	je     6ee54 <zfstack::buf::BlockPool::put+0x64>
   6ee27:	cmpq   $0x0,0x8(%rbx)
   6ee2c:	je     6ee37 <zfstack::buf::BlockPool::put+0x47>
   6ee2e:	mov    (%rbx),%rdi
   6ee31:	call   *0x45fc9(%rip)        # b4e00 <free@GLIBC_2.2.5>
   6ee37:	add    $0x18,%rbx
   6ee3b:	mov    %rbx,%rdi
   6ee3e:	add    $0x98,%rsp
   6ee45:	pop    %rbx
   6ee46:	pop    %r12
   6ee48:	pop    %r13
   6ee4a:	pop    %r14
   6ee4c:	pop    %r15
   6ee4e:	pop    %rbp
   6ee4f:	jmp    6e420 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   6ee54:	mov    0xf8(%rdi),%rdx
   6ee5b:	test   %rdx,%rdx
   6ee5e:	je     6ef95 <zfstack::buf::BlockPool::put+0x1a5>
   6ee64:	mov    0x10(%rdi),%rcx
   6ee68:	mov    0x28(%rdi),%rax
   6ee6c:	lea    (%rax,%rcx,1),%rsi
   6ee70:	cmp    %rdx,%rsi
   6ee73:	jne    6ef57 <zfstack::buf::BlockPool::put+0x167>
   6ee79:	cmpq   $0x800,0x8(%rbx)
   6ee81:	jne    6ee99 <zfstack::buf::BlockPool::put+0xa9>
