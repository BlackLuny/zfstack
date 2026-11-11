   73ec0:	push   %rbp
   73ec1:	push   %r15
   73ec3:	push   %r14
   73ec5:	push   %r13
   73ec7:	push   %r12
   73ec9:	push   %rbx
   73eca:	sub    $0x98,%rsp
   73ed1:	mov    %rsi,%rbx
   73ed4:	cmpb   $0x3,0xf3(%rdi)
   73edb:	je     73ef7 <zfstack::buf::BlockPool::put+0x37>
   73edd:	mov    0xb0(%rdi),%rax
   73ee4:	cmp    0x30(%rbx),%rax
   73ee8:	jne    73ef7 <zfstack::buf::BlockPool::put+0x37>
   73eea:	mov    0xb8(%rdi),%rax
   73ef1:	cmp    0x38(%rbx),%rax
   73ef5:	je     73f24 <zfstack::buf::BlockPool::put+0x64>
   73ef7:	cmpq   $0x0,0x8(%rbx)
   73efc:	je     73f07 <zfstack::buf::BlockPool::put+0x47>
   73efe:	mov    (%rbx),%rdi
   73f01:	call   *0x45ef9(%rip)        # b9e00 <free@GLIBC_2.2.5>
   73f07:	add    $0x18,%rbx
   73f0b:	mov    %rbx,%rdi
   73f0e:	add    $0x98,%rsp
   73f15:	pop    %rbx
   73f16:	pop    %r12
   73f18:	pop    %r13
   73f1a:	pop    %r14
   73f1c:	pop    %r15
   73f1e:	pop    %rbp
   73f1f:	jmp    734f0 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
   73f24:	mov    0xf8(%rdi),%rdx
   73f2b:	test   %rdx,%rdx
   73f2e:	je     74065 <zfstack::buf::BlockPool::put+0x1a5>
   73f34:	mov    0x10(%rdi),%rcx
   73f38:	mov    0x28(%rdi),%rax
   73f3c:	lea    (%rax,%rcx,1),%rsi
   73f40:	cmp    %rdx,%rsi
   73f43:	jne    74027 <zfstack::buf::BlockPool::put+0x167>
   73f49:	cmpq   $0x800,0x8(%rbx)
   73f51:	jne    73f69 <zfstack::buf::BlockPool::put+0xa9>
